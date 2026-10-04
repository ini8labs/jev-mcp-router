"""Tool-routing benchmark on a large real catalog (bench/catalog: 649 tools, 25 MCP servers).

    uv run python bench/run.py jev            # Jev: server questions + every tool question
    uv run python bench/run.py router [--no-hints]   # the shipped jevmcp.router.route(), end to end
    uv run python bench/run.py embed          # local embedding retrieval (bge-small)
    uv run python bench/run.py llm <model>    # an LLM picks tools from the catalog (cached prompt)
    uv run python bench/run.py report         # all routers side by side

Raw results are cached in bench/results/, so `report` re-scores without spending.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
load_dotenv(HERE.parent / ".env")

from jevmcp.catalog import Capability, load_hints  # noqa: E402
from jevmcp.jev import Jev  # noqa: E402
from jevmcp.router import route  # noqa: E402

RESULTS = HERE / "results"
DESC_CHARS = 300  # every router sees the same truncated description

SERVER_DESC = {
    "atlassian": "Jira (issues, sprints, boards, worklogs, SLAs, service desk) and Confluence (wiki pages, comments, attachments)",
    "aws_cloudwatch": "AWS CloudWatch: metrics, alarms, alarm history, Logs Insights queries over AWS log groups",
    "context7": "Up-to-date documentation and code examples for programming libraries and frameworks",
    "datadog": "Datadog: monitors, metrics, logs, APM traces, incidents, hosts, downtimes, RUM",
    "fetch": "Fetch a URL from the internet and return its content",
    "filesystem": "Local filesystem: read, write, search and list files and directories",
    "git": "Local git repository: status, diff, commit, branch, log",
    "github": "GitHub: repositories, code search, issues, pull requests, Actions CI runs, releases, security alerts",
    "google_maps": "Google Maps: geocoding, places, directions, distances",
    "grafana": "Grafana: dashboards, alerting, Loki logs, Tempo traces, Pyroscope profiles, OnCall, incidents, annotations",
    "kubernetes": "Kubernetes cluster: pods, deployments, logs, scaling, rollouts, helm charts, port-forward",
    "memory": "Persistent knowledge graph memory: remember entities, relations and observations",
    "mongodb": "MongoDB databases: collections, queries, aggregations, indexes, schemas",
    "notion": "Notion workspace: pages, databases, blocks, comments, search",
    "pagerduty": "PagerDuty: incidents, on-call schedules, escalation policies, services, change events, status pages",
    "playwright": "Browser automation: navigate web pages, click, type, take screenshots",
    "postgres": "PostgreSQL database: run read-only SQL queries",
    "prometheus": "Prometheus: PromQL metric queries, metric names and metadata, scrape targets",
    "sentry": "Sentry: application errors and exceptions, issues, events, AI root cause analysis (Seer)",
    "slack": "Slack: channels, post messages, thread replies, channel history, users",
    "supabase": "Supabase: projects, Postgres tables, SQL, migrations, logs, edge functions, branches",
    "terraform": "Terraform Registry: providers, modules, policies and their versions/docs",
    "time": "Current time and time zone conversion",
    "victorialogs": "VictoriaLogs: log search with LogsQL, log stats, log fields and streams",
    "victoriametrics": "VictoriaMetrics: MetricsQL queries, alerts, rules, cardinality, series, labels",
}


def load_catalog() -> list[dict]:
    out = []
    for f in sorted((HERE / "catalog").glob("*.json")):
        server = f.stem
        for t in json.loads(f.read_text()):
            desc = re.sub(r"\s+", " ", t["description"]).strip()[:DESC_CHARS]
            schema = {"name": f"{server}__{t['name']}", "description": t["description"], "parameters": t["input_schema"]}
            out.append({"id": f"{server}__{t['name']}", "server": server, "name": t["name"], "desc": desc,
                        "schema_tokens": len(json.dumps(schema)) // 4})
    return out


def load_requests() -> list[dict]:
    return [json.loads(l) for l in (HERE / "requests.jsonl").read_text().splitlines() if l.strip()]


def tool_q(t: dict) -> str:
    return f"To answer the user_request, the assistant should call {t['server']} tool '{t['name']}': {t['desc']}"


def server_q(s: str) -> str:
    return f"Answering the user_request requires using the {s} MCP server ({SERVER_DESC[s]})."


CONTEXT = "An assistant connected to many MCP servers (developer tools, observability, infrastructure, data, productivity)."


# ---------------------------------------------------------------- Jev

async def run_jev(tag: str = "jev"):
    cat, reqs = load_catalog(), load_requests()
    servers = sorted({t["server"] for t in cat})
    jev, sem = Jev(), asyncio.Semaphore(16)

    async def ask(state, qs):
        async with sem:
            r = await jev.decide(state, qs)
            return {"p": r.p_yes, "cost": r.cost_usd, "tokens": r.input_tokens, "latency": r.latency_s}

    async def one(i, req):
        state = {"user_request": req["request"], "context": CONTEXT}
        srv = ask(state, {s: server_q(s) for s in servers})
        per = [ask(state, {t["id"]: tool_q(t) for t in cat if t["server"] == s}) for s in servers]
        res = await asyncio.gather(srv, *per)
        print(f"  {i + 1}/{len(reqs)}", end="\r", flush=True)
        return {"servers": res[0], "tools": dict(zip(servers, res[1:]))}

    start = time.time()
    out = await asyncio.gather(*(one(i, r) for i, r in enumerate(reqs)))
    save(tag, out)
    print(f"\n{tag}: {len(out)} requests in {time.time() - start:.0f}s, "
          f"${sum(o['servers']['cost'] + sum(v['cost'] for v in o['tools'].values()) for o in out):.3f}")


# ---------------------------------------------------------------- the shipped router

def load_capabilities(hints: dict[str, str]) -> list[Capability]:
    caps = []
    for f in sorted((HERE / "catalog").glob("*.json")):
        for t in json.loads(f.read_text()):
            caps.append(Capability("tool", f.stem, t["name"], t["description"], t["input_schema"],
                                   hint=hints.get(f"{f.stem}__{t['name']}", "")))
    return caps


async def run_router(use_hints: bool = True):
    """jevmcp.router.route() exactly as the gateway calls it, once per request."""
    tag = "router" if use_hints else "router_nohints"
    caps = load_capabilities(load_hints(HERE.parent / "tool_hints.json") if use_hints else {})
    reqs, jev, sem = load_requests(), Jev(), asyncio.Semaphore(8)

    async def one(req):
        async with sem:
            d = await route(jev, req["request"], caps, SERVER_DESC)
        return {"tools": [c.id for c in d.tools], "servers": d.servers, "cost": d.jev.cost_usd, "latency": d.jev.latency_s}

    out = await asyncio.gather(*(one(r) for r in reqs))
    save(tag, out)
    print(f"{tag}: ${sum(o['cost'] for o in out):.4f} for {len(out)} requests")


# ---------------------------------------------------------------- embeddings

def run_embed():
    from fastembed import TextEmbedding
    import numpy as np

    cat, reqs = load_catalog(), load_requests()
    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    docs = np.array(list(model.embed([f"{t['server']} {t['name']}: {t['desc']}" for t in cat])))
    start = time.perf_counter()
    qs = np.array(list(model.query_embed([r["request"] for r in reqs])))
    per_query = (time.perf_counter() - start) / len(reqs)
    docs /= np.linalg.norm(docs, axis=1, keepdims=True)
    qs /= np.linalg.norm(qs, axis=1, keepdims=True)
    ranked = [[cat[j]["id"] for j in np.argsort(-(docs @ q))] for q in qs]
    save("embed", {"ranked": ranked, "latency": per_query})
    print(f"embed: ranked {len(cat)} tools for {len(reqs)} requests, {per_query * 1000:.0f} ms/query")


# ---------------------------------------------------------------- LLM routers

ROUTER_PROMPT = """You are a tool router. Given a user request and the catalog of MCP tools below, list every tool that must be called to fulfil the request, and nothing else. Prefer the fewest tools that fully cover the request. If no tool is needed, return an empty list.
Reply with only JSON: {"tools": ["<tool id>", ...]}

Catalog (tool id: description):
"""


async def run_llm(model: str):
    cat, reqs = load_catalog(), load_requests()
    catalog_text = ROUTER_PROMPT + "\n".join(f"{t['id']}: {t['desc']}" for t in cat)
    key = os.environ["OPENROUTER_API_KEY"]
    sem = asyncio.Semaphore(6)

    async def one(http, req):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": catalog_text, "cache_control": {"type": "ephemeral"}}]},
                # Haiku 4.5 ignores the system-prompt format and writes pseudo tool calls
                # unless the format is repeated here (Sonnet 5 and GPT-5 nano follow it either way).
                {"role": "user", "content": f"Request: {req['request']}\n\nDo not call any tools. "
                                            'Reply with only the JSON {"tools": [...]}.'
                 if "haiku" in model else req["request"]},
            ],
            "max_tokens": 4000,
            "usage": {"include": True},
        }
        if model.startswith("openai/"):
            body["reasoning"] = {"effort": "low"}
        async with sem:
            for attempt in range(4):
                t0 = time.perf_counter()
                r = await http.post("https://openrouter.ai/api/v1/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {key}"})
                if r.status_code < 300:
                    break
                await asyncio.sleep(2 * (attempt + 1))
            latency = time.perf_counter() - t0
        data = r.json()
        if "choices" not in data:
            return {"tools": [], "error": str(data)[:300], "cost": 0, "latency": latency}
        text = data["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            tools = json.loads(m.group(0)).get("tools", []) if m else []
        except (json.JSONDecodeError, AttributeError):
            tools = []
        tools = [t for t in tools if isinstance(t, str)]
        u = data.get("usage", {})
        return {"tools": tools, "cost": float(u.get("cost") or 0), "latency": latency,
                "prompt_tokens": u.get("prompt_tokens"), "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                "completion_tokens": u.get("completion_tokens")}

    async with httpx.AsyncClient(timeout=180) as http:
        first = await one(http, reqs[0])  # warms the prompt cache before the parallel burst
        rest = await asyncio.gather(*(one(http, r) for r in reqs[1:]))
    out = [first, *rest]
    save(f"llm_{model.replace('/', '_')}", out)
    errs = sum("error" in o for o in out)
    print(f"{model}: ${sum(o['cost'] for o in out):.3f} total, {errs} errors, "
          f"cached prompt tokens on request 2: {out[1].get('cached_tokens')}")


# ---------------------------------------------------------------- report

def save(name, obj):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(obj))


def load(name):
    p = RESULTS / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def normalize(sel, by_id, by_name):
    """LLMs sometimes drop or mangle the server prefix (confluence_create_page). Credit the
    choice when the tool name maps to exactly one catalog tool."""
    out = set()
    for s in sel:
        if s in by_id:
            out.add(s)
        elif len(by_name.get(s.split("__")[-1], [])) == 1:
            out.add(by_name[s.split("__")[-1]][0])
    return out


def score(name, reqs, cat, picks, costs, latencies, note=""):
    by_id = {t["id"]: t for t in cat}
    by_name: dict[str, list[str]] = {}
    for t in cat:
        by_name.setdefault(t["name"], []).append(t["id"])
    full = groups_ok = groups = extras = n_sel = schema = 0
    for req, sel in zip(reqs, picks):
        sel = normalize(sel, by_id, by_name)
        needed = {t for g in req["needs"] for t in g}
        ok = [bool(set(g) & sel) for g in req["needs"]]
        full += all(ok)
        groups_ok, groups = groups_ok + sum(ok), groups + len(ok)
        extras += len(sel - needed)
        n_sel += len(sel)
        schema += sum(by_id[s]["schema_tokens"] for s in sel)
    n = len(reqs)
    return {"router": name, "all_needed": full / n, "group_recall": groups_ok / max(1, groups),
            "tools_selected": n_sel / n, "extras": extras / n, "schema_tokens": schema / n,
            "cost": sum(costs) / n, "latency_p50": statistics.median(latencies), "note": note}


def report():
    cat, reqs = load_catalog(), load_requests()
    servers = sorted({t["server"] for t in cat})
    full_schema = sum(t["schema_tokens"] for t in cat)
    rows = []

    jev = load("jev")
    if jev:
        flat = [[t for s in servers for t, p in o["tools"][s]["p"].items() if p >= 0.5] for o in jev]
        rows.append(score("Jev flat (649 yes/no)", reqs, cat, flat,
                          [sum(v["cost"] for v in o["tools"].values()) for o in jev],
                          [max(v["latency"] for v in o["tools"].values()) for o in jev]))
        for sth in (0.5, 0.3, 0.2):
            picks, costs, lats = [], [], []
            for o in jev:
                keep = [s for s in servers if o["servers"]["p"][s] >= sth]
                picks.append([t for s in keep for t, p in o["tools"][s]["p"].items() if p >= 0.5])
                costs.append(o["servers"]["cost"] + sum(o["tools"][s]["cost"] for s in keep))
                lats.append(o["servers"]["latency"] + max([o["tools"][s]["latency"] for s in keep] or [0]))
            rows.append(score(f"Jev 2-step (server>={sth})", reqs, cat, picks, costs, lats))
        # Same answers, one extra rule: a server Jev said YES to (>=0.5) but none of whose
        # tools cleared 0.5 contributes its top-ranked tool. Fixes weakly described tools.
        for sth in (0.3,):
            picks, costs, lats = [], [], []
            for o in jev:
                keep = [s for s in servers if o["servers"]["p"][s] >= sth]
                sel = []
                for s in keep:
                    ps = o["tools"][s]["p"]
                    yes = [t for t, p in ps.items() if p >= 0.5]
                    if not yes and o["servers"]["p"][s] >= 0.5:
                        yes = [max(ps, key=ps.get)]
                    sel += yes
                picks.append(sel)
                costs.append(o["servers"]["cost"] + sum(o["tools"][s]["cost"] for s in keep))
                lats.append(o["servers"]["latency"] + max([o["tools"][s]["latency"] for s in keep] or [0]))
            rows.append(score(f"Jev 2-step + top-1 fallback", reqs, cat, picks, costs, lats))

    emb = load("embed")
    if emb:
        for k in (5, 10, 20):
            rows.append(score(f"Embeddings top-{k}", reqs, cat, [r[:k] for r in emb["ranked"]],
                              [0.0] * len(reqs), [emb["latency"]] * len(reqs), "local model, ~free"))
        if jev:
            for k in (30, 60):
                picks, costs = [], []
                for o, ranked in zip(jev, emb["ranked"]):
                    top = set(ranked[:k])
                    p_all = {t: p for v in o["tools"].values() for t, p in v["p"].items()}
                    picks.append([t for t in top if p_all[t] >= 0.5])
                    costs.append(sum(v["cost"] for v in o["tools"].values()) * k / len(cat))
                rows.append(score(f"Embeddings top-{k} -> Jev", reqs, cat, picks, costs,
                                  [emb["latency"] + 0.5] * len(reqs), "cost/latency estimated"))

    for tag, label in (("router_nohints", "Jev router, shipped (no hints)"), ("router", "Jev router, shipped + hints")):
        out = load(tag)
        if out:
            rows.append(score(label, reqs, cat, [o["tools"] for o in out], [o["cost"] for o in out],
                              [o["latency"] for o in out], "latency at 8 concurrent"))

    for f in sorted(RESULTS.glob("llm_*.json")):
        out = json.loads(f.read_text())
        name = f.stem[4:].replace("_", "/", 1)
        rows.append(score(f"LLM router {name}", reqs, cat, [o["tools"] for o in out],
                          [o["cost"] for o in out], [o["latency"] for o in out], "catalog prompt cached"))

    print(f"{len(reqs)} requests, {len(cat)} tools, {len(servers)} servers; "
          f"all schemas = ~{full_schema:,} tokens if sent to the LLM\n")
    hdr = f"{'router':32} {'all needed':>10} {'group rec':>9} {'#tools':>7} {'extras':>7} {'schema tok':>10} {'$/request':>10} {'p50 s':>6}"
    print(hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r['router']:32} {r['all_needed']:>10.0%} {r['group_recall']:>9.0%} {r['tools_selected']:>7.1f} "
              f"{r['extras']:>7.1f} {r['schema_tokens']:>10,.0f} {r['cost']:>10.5f} {r['latency_p50']:>6.2f}  {r['note']}")
    save("report", rows)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "jev":
        asyncio.run(run_jev())
    elif cmd == "router":
        asyncio.run(run_router(use_hints="--no-hints" not in sys.argv))
    elif cmd == "embed":
        run_embed()
    elif cmd == "llm":
        asyncio.run(run_llm(sys.argv[2]))
    else:
        report()
