"""Build a large, real tool catalog: start each MCP server with dummy credentials,
call tools/list, save its tools (exact names, descriptions, schemas) and exit.
No tool is ever called, so dummy credentials are enough for most servers.

    uv run python bench/collect.py            # writes bench/catalog/<server>.json
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

HERE = Path(__file__).parent
BIN = HERE / "bin"
OUT = HERE / "catalog"
D = "dummy-token-for-listing-only"

SERVERS = {
    # observability
    "prometheus": ("uvx", ["prometheus-mcp-server"], {"PROMETHEUS_URL": "http://localhost:9090"}),
    "victorialogs": (str(BIN / "mcp-victorialogs"), [], {"VL_INSTANCE_ENTRYPOINT": "http://localhost:9428"}),
    "victoriametrics": (str(BIN / "mcp-victoriametrics"), [], {"VM_INSTANCE_ENTRYPOINT": "http://localhost:8428", "VM_INSTANCE_TYPE": "single"}),
    "grafana": (str(BIN / "mcp-grafana"), [], {"GRAFANA_URL": "http://localhost:3000", "GRAFANA_API_KEY": D}),
    "sentry": ("npx", ["-y", "@sentry/mcp-server@latest", f"--access-token={D}"], {"SENTRY_ACCESS_TOKEN": D}),
    "datadog": ("npx", ["-y", "@winor30/mcp-server-datadog"], {"DATADOG_API_KEY": D, "DATADOG_APP_KEY": D}),
    "pagerduty": ("uvx", ["pagerduty-mcp", "--enable-write-tools"], {"PAGERDUTY_USER_API_KEY": D}),
    # infra
    "kubernetes": ("npx", ["-y", "mcp-server-kubernetes"], {}),
    "terraform": (str(BIN / "terraform-mcp-server"), ["stdio"], {}),
    "aws_cloudwatch": ("uvx", ["awslabs.cloudwatch-mcp-server@latest"], {"AWS_REGION": "us-east-1"}),
    # code and work tracking
    "github": (str(BIN / "github-mcp-server"), ["stdio", "--toolsets=all"], {"GITHUB_PERSONAL_ACCESS_TOKEN": D}),
    "gitlab": ("npx", ["-y", "@modelcontextprotocol/server-gitlab"], {"GITLAB_PERSONAL_ACCESS_TOKEN": D}),
    "git": ("uvx", ["mcp-server-git"], {}),
    "atlassian": ("uvx", ["mcp-atlassian"], {"JIRA_URL": "https://example.atlassian.net", "JIRA_USERNAME": "a@b.c",
                                             "JIRA_API_TOKEN": D, "CONFLUENCE_URL": "https://example.atlassian.net/wiki",
                                             "CONFLUENCE_USERNAME": "a@b.c", "CONFLUENCE_API_TOKEN": D}),
    "notion": ("npx", ["-y", "@notionhq/notion-mcp-server"], {"NOTION_TOKEN": D}),
    "slack": ("npx", ["-y", "@modelcontextprotocol/server-slack"], {"SLACK_BOT_TOKEN": D, "SLACK_TEAM_ID": "T000"}),
    # data
    "postgres": ("npx", ["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/db"], {}),
    "mongodb": ("npx", ["-y", "mongodb-mcp-server"], {}),
    "supabase": ("npx", ["-y", "@supabase/mcp-server-supabase@latest", f"--access-token={D}"], {}),
    "stripe": ("npx", ["-y", "@stripe/mcp", "--tools=all", "--api-key=sk_test_dummy"], {}),
    # general
    "filesystem": ("npx", ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], {}),
    "playwright": ("npx", ["-y", "@playwright/mcp@latest"], {}),
    "fetch": ("uvx", ["mcp-server-fetch"], {}),
    "time": ("uvx", ["mcp-server-time"], {}),
    "memory": ("npx", ["-y", "@modelcontextprotocol/server-memory"], {}),
    "context7": ("npx", ["-y", "@upstash/context7-mcp"], {}),
    "google_maps": ("npx", ["-y", "@modelcontextprotocol/server-google-maps"], {"GOOGLE_MAPS_API_KEY": D}),
}


async def collect(name: str, spec) -> str:
    cmd, args, env = spec
    out = OUT / f"{name}.json"
    if out.exists():
        return f"{name}: cached"
    params = StdioServerParameters(command=cmd, args=args, env={**os.environ, **env}, cwd="/tmp")
    try:
        async with asyncio.timeout(240):
            async with Client(params) as c:
                tools = (await c.list_tools()).tools
    except BaseException as e:  # noqa: BLE001 - a server failing to start is expected for some
        return f"{name}: FAILED {type(e).__name__}: {str(e)[:150]}"
    out.write_text(json.dumps([{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                               for t in tools], indent=1))
    return f"{name}: {len(tools)} tools"


async def main():
    OUT.mkdir(exist_ok=True)
    only = set(sys.argv[1:])
    sem = asyncio.Semaphore(6)

    async def one(n, s):
        async with sem:
            print(await collect(n, s), flush=True)

    await asyncio.gather(*(one(n, s) for n, s in SERVERS.items() if not only or n in only))


if __name__ == "__main__":
    asyncio.run(main())
