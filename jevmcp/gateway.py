"""MCP gateway: put this in front of your real MCP servers.

Your MCP client (Claude Code, Claude Desktop, your own agent) connects only to
this server and sees two small tools instead of every Prometheus and
VictoriaLogs tool:

  jev_route(request)          Jev's YES/NO decision. Returns the full schemas of
                              the YES tools only, plus the YES skills' instructions.
  jev_call(tool, arguments)   Calls a tool on the underlying MCP server.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

from .catalog import Capability, Hub, load_server_config, load_skills
from .jev import make_jev
from .router import route

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class State:
    hub: Hub
    skills: list[Capability]
    jev: object


@asynccontextmanager
async def lifespan(_server: MCPServer):
    async with Hub(load_server_config(os.getenv("JEV_MCP_SERVERS") or ROOT / "mcp_servers.json"), ROOT) as hub:
        yield State(hub, load_skills(ROOT / "skills"), make_jev())


mcp = MCPServer(
    "jev-router",
    instructions="Always call jev_route with the user's request first. It tells you exactly which tools to call "
                 "(with their schemas) and which playbook to follow. Then call each of them via jev_call.",
    lifespan=lifespan,
)


@mcp.tool()
async def jev_route(request: str, ctx: Context) -> str:
    """Decide which tools (across all connected MCP servers) and playbooks a request needs.
    Returns only the YES tools with their input schemas, and the instructions of each YES playbook."""
    st: State = ctx.request_context.lifespan_context
    d = await route(st.jev, request, st.hub.tools + st.skills, st.hub.descriptions)
    return json.dumps({
        "call_these_tools": [{"tool": c.id, "description": c.description, "input_schema": c.input_schema} for c in d.tools],
        "follow_these_playbooks": [{"name": s.name, "instructions": s.body} for s in d.skills],
        "not_needed_count": len(d.rejected),  # not listed: at hundreds of tools the list itself is thousands of tokens
        "jev_cost_usd": round(d.jev.cost_usd, 8),
    }, indent=1)


@mcp.tool()
async def jev_call(tool: str, arguments: dict, ctx: Context) -> str:
    """Call a tool chosen by jev_route. `tool` is the id from jev_route (e.g. prometheus__execute_range_query)."""
    st: State = ctx.request_context.lifespan_context
    return await st.hub.call(tool, arguments)


def main() -> None:
    mcp.run()
