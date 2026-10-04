"""python -m jevmcp <command>

  catalog                 what Jev decides over, and what it costs to show it all to an LLM
  route   "request"       Jev's YES/NO per tool and skill (no LLM)
  run     "request"       Jev route -> LLM fills args for YES tools -> tools run -> answer
          --compare       also run the LLM-only baseline and print the cost difference
  eval    [file]          routing accuracy and cost over labeled requests
  serve                   run the MCP gateway over stdio
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from .agent import LOAD_SKILL, Run, fn_spec, run_baseline, run_with_jev  # noqa: E402
from .catalog import Hub, load_server_config, load_skills  # noqa: E402
from .jev import make_jev  # noqa: E402
from .llm import estimate_tokens, make_llm, price  # noqa: E402
from .router import route  # noqa: E402

B, D, G, R, Y, X = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def hub() -> Hub:
    return Hub(load_server_config(os.getenv("JEV_MCP_SERVERS") or ROOT / "mcp_servers.json"), ROOT)


def skills():
    return load_skills(ROOT / "skills")


async def cmd_catalog(_):
    async with hub() as h:
        sk = skills()
        for c in h.tools:
            print(f"  tool   {c.id:42} {D}{c.description[:70]}{X}")
        for s in sk:
            print(f"  skill  {s.name:42} {D}{s.description[:70]}{X}")
        all_schemas = [fn_spec(c) for c in h.tools] + [LOAD_SKILL]
        toks = estimate_tokens(all_schemas) + estimate_tokens("\n".join(f"- {s.name}: {s.description}" for s in sk))
        print(f"\n{len(h.tools)} tools, {len(sk)} skills. Showing all of them to an LLM costs ~{toks} input tokens "
              f"on every turn, before the user's request.")


async def cmd_route(a):
    async with hub() as h:
        sk = skills()
        d = await route(make_jev(), a.request, h.tools + sk, h.descriptions)
        print_decision(d, a.debug)


def print_decision(d, debug=False):
    print(f"{B}Jev decision{X}{f' {Y}(FAKE: offline keyword stand-in){X}' if d.jev.fake else ''}")
    for c in d.tools + d.skills:
        print(f"  {G}YES{X}  {c.kind:5}  {c.id if c.kind == 'tool' else c.name}"
              + (f"  {D}{_p(d, c)}{X}" if debug else ""))
    if debug:
        for c in d.rejected:
            print(f"  {R}NO {X}  {c.kind:5}  {c.id if c.kind == 'tool' else c.name}  {D}{_p(d, c)}{X}")
    else:
        print(f"  {R}NO {X}  {len(d.rejected)} others")
    if d.servers is not None:
        print(f"  {D}2-step: servers kept after step 1: {d.servers}{X}")
    print(f"  {D}{d.jev.requests} Jev request, {d.jev.input_tokens} tokens, ${d.jev.cost_usd:.6f}, "
          f"{d.jev.latency_s * 1000:.0f} ms{X}")


def _p(d, c) -> str:
    p = d.jev.p_yes.get(c.id)
    return f"p={p:.2f}" if p is not None else "server ruled out in step 1"


def print_run(r: Run):
    print(f"\n{B}== {r.mode} =={X}" + (f"  {Y}(token counts estimated, offline){X}" if r.estimated else ""))
    if r.decision:
        print_decision(r.decision)
    for s in r.steps:
        if s.who == "llm":
            print(f"  llm   {s.detail}  {D}in={s.input_tokens} out={s.output_tokens} ${s.cost_usd:.5f}{X}")
        elif s.who == "tool":
            print(f"  tool  {s.detail[:150]}")
    print(f"\n{r.answer.strip()}\n")
    print(f"  {B}total ${r.cost_usd:.5f}{X}  (LLM input tokens: {r.llm_input_tokens})")


async def cmd_run(a):
    llm, jev = make_llm(), make_jev()
    async with hub() as h:
        sk = skills()
        r = await run_with_jev(a.request, jev, llm, h, sk)
        print_run(r)
        if a.compare:
            b = await run_baseline(a.request, llm, h, sk)
            print_run(b)
            saved = b.cost_usd - r.cost_usd
            print(f"\n{B}Jev path ${r.cost_usd:.5f} vs LLM-only ${b.cost_usd:.5f}: "
                  f"{G}saved ${saved:.5f} ({saved / b.cost_usd:.0%}){X}, "
                  f"LLM input tokens {r.llm_input_tokens} vs {b.llm_input_tokens}")


async def cmd_eval(a):
    rows = [json.loads(l) for l in Path(a.file).read_text().splitlines() if l.strip()]
    jev = make_jev()
    async with hub() as h:
        sk = skills()
        decisions = await asyncio.gather(*(route(jev, row["request"], h.tools + sk, h.descriptions) for row in rows))
        all_schema_toks = estimate_tokens([fn_spec(c) for c in h.tools])
        pin, _ = price(make_llm().model)
    # A missed tool means a worse answer; an extra one only costs its schema tokens.
    # So a row passes when nothing needed was missed, and extras are reported separately.
    passed = exact = tp = fp = fn = 0
    jev_cost = saved_toks = 0.0
    for row, d in zip(rows, decisions):
        got = {c.id for c in d.tools} | {f"skill:{s.name}" for s in d.skills}
        want = set(row["tools"]) | {f"skill:{s}" for s in row["skills"]}
        passed += not (want - got)
        exact += got == want
        tp, fp, fn = tp + len(got & want), fp + len(got - want), fn + len(want - got)
        jev_cost += d.jev.cost_usd
        saved_toks += all_schema_toks - estimate_tokens([fn_spec(c) for c in d.tools])
        mark = f"{R}MISS{X}" if want - got else (f"{G}ok  {X}" if got == want else f"{Y}+{len(got - want)}  {X}")
        print(f"{mark} {row['request'][:70]:70}")
        if want - got:
            print(f"      {D}missing {sorted(want - got)}{X}")
        if got - want:
            print(f"      {D}extra   {sorted(got - want)}{X}")
    n = len(rows)
    fake = f" {Y}(FAKE Jev: these numbers say nothing about the real model){X}" if decisions[0].jev.fake else ""
    print(f"\n{B}{passed}/{n} with nothing needed missed{X} ({exact} exact)  recall {tp / max(1, tp + fn):.2f}  "
          f"precision {tp / max(1, tp + fp):.2f}  extras/request {fp / n:.1f}{fake}")
    print(f"Jev cost ${jev_cost / n:.7f}/request.  Tool-schema tokens kept out of the LLM: ~{saved_toks / n:.0f}/turn "
          f"= ${saved_toks / n * pin / 1e6:.5f}/turn at ${pin}/MTok")


def main():
    p = argparse.ArgumentParser(prog="python -m jevmcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    s = sub.add_parser("route"); s.add_argument("request"); s.add_argument("--debug", action="store_true")
    s = sub.add_parser("run"); s.add_argument("request"); s.add_argument("--compare", action="store_true")
    s = sub.add_parser("eval"); s.add_argument("file", nargs="?", default=str(ROOT / "evals/queries.jsonl"))
    sub.add_parser("serve")
    a = p.parse_args()
    if a.cmd == "serve":
        from .gateway import main as serve
        return serve()
    try:
        asyncio.run({"catalog": cmd_catalog, "route": cmd_route, "run": cmd_run, "eval": cmd_eval}[a.cmd](a))
    except Exception as e:  # surface API errors (e.g. no credits) as one line
        sys.exit(f"{R}error:{X} {e}")
