"""Run a request end to end, two ways, and account for every token.

  jev       Jev says YES/NO per tool and skill -> LLM sees only the YES tools and
            the YES skills' instructions -> it fills arguments -> tools run -> answer.
  baseline  The usual MCP setup: LLM sees every tool schema and the skill index
            on every turn and decides everything itself.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from .catalog import Capability, Hub
from .router import RouteDecision, route

BASE_SYSTEM = (
    "You are an SRE assistant with access to Prometheus (metrics, PromQL) and VictoriaLogs (logs, LogsQL). "
    "Call tools to gather evidence, then answer concisely with numbers and timestamps from the results. "
    "Call independent tools in parallel in one turn."
)
LOAD_SKILL = {"type": "function", "function": {
    "name": "load_skill", "description": "Load the full instructions of a skill (playbook) by name.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}


def fn_spec(c: Capability) -> dict:
    return {"type": "function", "function": {"name": c.id, "description": c.description,
                                             "parameters": c.input_schema or {"type": "object", "properties": {}}}}


@dataclass
class Step:
    who: str                # "jev" | "llm" | "tool"
    detail: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Run:
    mode: str
    request: str
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    decision: RouteDecision | None = None
    estimated: bool = False

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.steps)

    @property
    def llm_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps if s.who == "llm")


async def _loop(run: Run, llm, hub: Hub, system: str, tools: list[dict], skills: list[Capability],
                max_turns: int = 8) -> Run:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": run.request}]
    for turn in range(1, max_turns + 1):
        res = await llm.chat(messages, tools)
        run.estimated |= res.estimated
        run.steps.append(Step("llm", f"turn {turn}: {len(tools)} tool schemas in context, "
                                     f"{len(res.tool_calls)} tool calls", res.input_tokens, res.output_tokens, res.cost_usd))
        messages.append(res.raw_message)
        if not res.tool_calls:
            run.answer = res.content
            return run

        async def execute(call: dict) -> str:
            if call["name"] == "load_skill":
                s = next((s for s in skills if s.name == call["arguments"].get("name")), None)
                return s.body if s else "unknown skill"
            return await hub.call(call["name"], call["arguments"])

        results = await asyncio.gather(*(execute(c) for c in res.tool_calls))
        for call, out in zip(res.tool_calls, results):
            run.tools_called.append(call["name"])
            run.steps.append(Step("tool", f"{call['name']}({json.dumps(call['arguments'])})"))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:6000]})
    run.answer = "(stopped: max turns reached)"
    return run


async def run_with_jev(request: str, jev, llm, hub: Hub, skills: list[Capability]) -> Run:
    run = Run("jev", request)
    d = await route(jev, request, hub.tools + skills, hub.descriptions)
    run.decision = d
    run.steps.append(Step("jev", f"{len(hub.tools) + len(skills)} yes/no questions -> "
                                 f"YES: {[c.id for c in d.tools + d.skills]}", d.jev.input_tokens, 0, d.jev.cost_usd))
    system = BASE_SYSTEM
    if d.tools:
        system += ("\nThe tools you have were pre-selected for this request. Call the ones that help answer it, "
                   "with suitable arguments, in parallel.")
    for s in d.skills:
        system += f"\n\n## Playbook: {s.name}\n{s.body}"
    return await _loop(run, llm, hub, system, [fn_spec(c) for c in d.tools], skills)


async def run_baseline(request: str, llm, hub: Hub, skills: list[Capability]) -> Run:
    run = Run("baseline", request)
    system = BASE_SYSTEM
    if skills:
        system += "\n\nAvailable skills (call load_skill to read one before using it):\n" + "\n".join(
            f"- {s.name}: {s.description}" for s in skills)
    tools = [fn_spec(c) for c in hub.tools] + ([LOAD_SKILL] if skills else [])
    return await _loop(run, llm, hub, system, tools, skills)
