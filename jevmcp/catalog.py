"""The catalog Jev decides over: tools from MCP servers, plus skills.

`Hub` keeps one MCP client open per configured server and can call tools on
them. Servers are configured in the same shape as Claude Desktop / Claude
Code's `mcpServers` block (command/args/env for stdio, or url for HTTP).
"""

from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters


@dataclass
class Capability:
    kind: str                 # "tool" | "skill"
    server: str               # MCP server name, or "skills"
    name: str                 # tool / skill name as the server knows it
    description: str
    input_schema: dict = field(default_factory=dict)
    body: str = ""            # skill instructions (SKILL.md without frontmatter)
    hint: str = ""            # routing-only description from tool_hints.json

    @property
    def id(self) -> str:      # unique, and valid as an LLM function name
        return f"{self.server}__{self.name}"

    def routing_text(self, max_chars: int) -> str:
        """What Jev sees: the hint if there is one, else the description, whitespace collapsed."""
        return self.hint or re.sub(r"\s+", " ", self.description).strip()[:max_chars]

    def params_summary(self) -> str:
        props = self.input_schema.get("properties", {})
        return ", ".join(props) if props else "none"


def load_skills(skills_dir: str | Path) -> list[Capability]:
    """Each skill is <dir>/<name>/SKILL.md with `name` and `description` frontmatter."""
    out = []
    for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
        text = path.read_text()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
        out.append(Capability("skill", "skills", meta.get("name", path.parent.name),
                              meta.get("description", "").strip(), body=m.group(2).strip()))
    return out


def load_server_config(path: str | Path) -> dict[str, dict]:
    raw = json.loads(Path(path).read_text())
    return raw.get("mcpServers", raw)


def load_hints(path: str | Path) -> dict[str, str]:
    p = Path(path)
    return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")} if p.exists() else {}


class Hub:
    """Open connections to every configured MCP server."""

    def __init__(self, servers: dict[str, dict], base_dir: Path | None = None, hints: dict[str, str] | None = None):
        self.servers = servers
        self.base_dir = base_dir or Path.cwd()
        self.hints = hints if hints is not None else load_hints(self.base_dir / "tool_hints.json")
        # Optional "description" per server in mcp_servers.json; used by two-step routing.
        self.descriptions = {n: c["description"] for n, c in servers.items() if c.get("description")}
        self.clients: dict[str, Client] = {}
        self.tools: list[Capability] = []
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "Hub":
        await self._stack.__aenter__()
        for name, cfg in self.servers.items():
            if "url" in cfg:
                target: Any = cfg["url"]
            else:
                target = StdioServerParameters(
                    command=cfg["command"], args=cfg.get("args", []),
                    env={**os.environ, **cfg.get("env", {})}, cwd=cfg.get("cwd", str(self.base_dir)),
                )
            client = await self._stack.enter_async_context(Client(target))
            self.clients[name] = client
            listed = await client.list_tools()
            for t in listed.tools:
                self.tools.append(Capability("tool", name, t.name, (t.description or "").strip(),
                                             t.input_schema or {"type": "object", "properties": {}},
                                             hint=self.hints.get(f"{name}__{t.name}", "")))
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.__aexit__(*exc)

    def find(self, cap_id: str) -> Capability | None:
        return next((t for t in self.tools if t.id == cap_id), None)

    async def call(self, cap_id: str, arguments: dict) -> str:
        cap = self.find(cap_id)
        if cap is None:
            return f"error: unknown tool {cap_id}"
        res = await self.clients[cap.server].call_tool(cap.name, arguments)
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "text", None))
        return ("error: " if res.is_error else "") + (text or json.dumps(res.structured_content or {}))
