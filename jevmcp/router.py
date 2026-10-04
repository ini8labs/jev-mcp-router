"""Jev as the tool/skill selection layer.

Small catalogs (one Jev request's worth) are routed in one step: a yes/no
question per tool and per skill. Large catalogs are routed in two:

  1. one yes/no per MCP server (plus the skill questions)
  2. one yes/no per tool, only for servers that got p >= SERVER_P

with a fallback: a server Jev clearly needs (p >= FALLBACK_SERVER_P) but none
of whose tools reached YES contributes its top-ranked tool. On 649 real tools
from 25 servers (bench/) this picked every needed tool for 94% of requests at
~1.3 extra tools, ~$0.0003 and ~0.8 s per request.

No LLM is involved and no tool schemas are sent, only names and one-line
descriptions (or the routing hints from tool_hints.json).
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Capability
from .jev import MAX_QUESTIONS_PER_REQUEST, JevResult

SERVER_P = 0.3           # step 1: keep servers at or above this (tuned for recall)
FALLBACK_SERVER_P = 0.5  # step 2: a server this certain always contributes a tool
DESC_CHARS = 300

CONTEXT = "An assistant connected to MCP servers (tools) and written playbooks (skills)."


# Phrasing matters more than anything else here. "should call" never missed a needed
# tool on the small eval; "requires calling ..." was stricter and missed some.
def tool_question(c: Capability) -> str:
    return f"To answer the user_request, the assistant should call {c.server} tool '{c.name}': {c.routing_text(DESC_CHARS)}"


def server_question(server: str, description: str) -> str:
    return f"Answering the user_request requires using the {server} MCP server ({description})."


def skill_question(c: Capability) -> str:
    return f"The playbook '{c.name}' applies to the user_request and should be followed. The playbook: {c.description}"


@dataclass
class RouteDecision:
    request: str
    tools: list[Capability]        # YES
    skills: list[Capability]       # YES
    rejected: list[Capability]     # NO (including tools of servers ruled out in step 1)
    jev: JevResult
    servers: list[str] | None = None  # step-1 YES servers; None when routed in one step

    def as_dict(self) -> dict:
        return {
            "request": self.request,
            "servers": self.servers,
            "call_tools": [c.id for c in self.tools],
            "use_skills": [c.name for c in self.skills],
            "jev": {"requests": self.jev.requests, "input_tokens": self.jev.input_tokens,
                    "cost_usd": round(self.jev.cost_usd, 8), "latency_s": round(self.jev.latency_s, 3),
                    "fake": self.jev.fake},
        }


async def route(jev, request: str, catalog: list[Capability],
                server_descriptions: dict[str, str] | None = None, two_step: bool | None = None) -> RouteDecision:
    tools = [c for c in catalog if c.kind == "tool"]
    skills = [c for c in catalog if c.kind == "skill"]
    state = {"user_request": request, "context": CONTEXT}
    if two_step is None:
        two_step = len(catalog) > MAX_QUESTIONS_PER_REQUEST

    if not two_step:
        res = await jev.decide(state, {c.id: tool_question(c) if c.kind == "tool" else skill_question(c) for c in catalog})
        yes = {k for k, v in res.yes.items() if v}
        return _decision(request, catalog, yes, res, None)

    servers = sorted({c.server for c in tools})
    descs = server_descriptions or {}
    step1 = await jev.decide(state, {
        **{f"server:{s}": server_question(s, descs.get(s) or _auto_description(s, tools)) for s in servers},
        **{c.id: skill_question(c) for c in skills},
    })
    keep = [s for s in servers if step1.p_yes[f"server:{s}"] >= SERVER_P]
    candidates = [c for c in tools if c.server in keep]
    step2 = await jev.decide(state, {c.id: tool_question(c) for c in candidates}) if candidates else None

    yes = {c.id for c in skills if step1.yes.get(c.id)}
    if step2:
        yes |= {k for k, v in step2.yes.items() if v}
        for s in keep:
            own = {c.id: step2.p_yes[c.id] for c in candidates if c.server == s}
            if step1.p_yes[f"server:{s}"] >= FALLBACK_SERVER_P and own and not (yes & own.keys()):
                yes.add(max(own, key=own.get))
    return _decision(request, catalog, yes, _merge(step1, step2), keep)


def _decision(request, catalog, yes, res, servers) -> RouteDecision:
    return RouteDecision(
        request=request,
        tools=[c for c in catalog if c.kind == "tool" and c.id in yes],
        skills=[c for c in catalog if c.kind == "skill" and c.id in yes],
        rejected=[c for c in catalog if c.id not in yes],
        jev=res,
        servers=servers,
    )


def _auto_description(server: str, tools: list[Capability]) -> str:
    """Used when mcp_servers.json gives no description: the server's own tool names."""
    names = [c.name for c in tools if c.server == server]
    return "tools: " + ", ".join(names[:25]) + (" ..." if len(names) > 25 else "")


def _merge(a: JevResult, b: JevResult | None) -> JevResult:
    if b is None:
        return a
    return JevResult(
        yes={**a.yes, **b.yes}, p_yes={**a.p_yes, **b.p_yes},
        input_tokens=a.input_tokens + b.input_tokens, cost_usd=a.cost_usd + b.cost_usd,
        latency_s=a.latency_s + b.latency_s,  # sequential steps
        requests=a.requests + b.requests, fake=a.fake, errors=a.errors + b.errors,
    )
