"""Mock of VictoriaMetrics mcp-victorialogs: same tool names and parameters,
canned logs for the same incident as the Prometheus mock. Swap for the real
server in mcp_servers.real.json."""

import json

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("victorialogs")

LOGS = [
    {"_time": "2026-10-04T14:01:58Z", "service": "payments", "level": "warn", "_msg": "db pool: 48/50 connections in use"},
    {"_time": "2026-10-04T14:02:11Z", "service": "payments", "level": "error", "_msg": "timeout acquiring connection from pool payments-db after 2000ms"},
    {"_time": "2026-10-04T14:02:12Z", "service": "checkout", "level": "error", "_msg": "POST /checkout 502: upstream payments timed out"},
    {"_time": "2026-10-04T14:04:40Z", "service": "payments", "level": "error", "_msg": "timeout acquiring connection from pool payments-db after 2000ms"},
    {"_time": "2026-10-04T14:06:03Z", "service": "payments", "level": "error", "_msg": "slow query 4.8s: SELECT ... FROM ledger_entries WHERE account_id = $1 (seq scan)"},
    {"_time": "2026-10-04T14:09:51Z", "service": "checkout", "level": "error", "_msg": "POST /checkout 502: upstream payments timed out"},
    {"_time": "2026-10-04T14:17:30Z", "service": "payments", "level": "info", "_msg": "deploy payments v2.14.1 rolled back to v2.14.0"},
    {"_time": "2026-10-04T14:19:02Z", "service": "payments", "level": "info", "_msg": "db pool: 21/50 connections in use"},
    {"_time": "2026-10-04T13:58:00Z", "service": "payments", "level": "info", "_msg": "deploy payments v2.14.1 started (migration: drop index ledger_entries_account_id)"},
    {"_time": "2026-10-04T14:05:00Z", "service": "auth", "level": "info", "_msg": "token refresh ok"},
]


def _match(query: str) -> list[dict]:
    q = query.lower()
    out = [l for l in LOGS if not any(s in q for s in ("checkout", "payments", "auth", "cart", "search")) or l["service"] in q]
    if "error" in q:
        out = [l for l in out if l["level"] == "error"]
    if "deploy" in q:
        out = [l for l in LOGS if "deploy" in l["_msg"]]
    return out


@mcp.tool()
def query(query: str, limit: int = 100, start: str | None = None, end: str | None = None) -> str:
    """Execute a LogsQL query against VictoriaLogs and return matching log entries (raw log lines with fields)."""
    return json.dumps(_match(query)[:limit])


@mcp.tool()
def hits(query: str, start: str, end: str, step: str = "5m") -> str:
    """Return the number of matching log entries grouped by time buckets. Use to see when log volume or errors spiked."""
    m = _match(query)
    return json.dumps({"query": query, "buckets": [{"time": "14:00", "hits": sum('T14:0' in l["_time"] for l in m) * 120},
                                                   {"time": "14:10", "hits": sum('T14:1' in l["_time"] for l in m) * 120},
                                                   {"time": "13:50", "hits": sum('T13:5' in l["_time"] for l in m) * 3}]})


@mcp.tool()
def facets(query: str, start: str | None = None, end: str | None = None) -> str:
    """Return the most frequent values for each log field in the matching logs (e.g. which service or level dominates)."""
    m = _match(query)
    count = lambda f: {v: sum(l[f] == v for l in m) for v in {l[f] for l in m}}
    return json.dumps({"service": count("service"), "level": count("level")})


@mcp.tool()
def field_names(query: str, start: str | None = None, end: str | None = None) -> str:
    """List the field names present in logs matching the query."""
    return json.dumps(["_time", "_msg", "_stream", "service", "level", "trace_id", "pod"])


@mcp.tool()
def field_values(field: str, query: str = "*", limit: int = 20) -> str:
    """List distinct values of one log field for logs matching the query."""
    return json.dumps(sorted({l.get(field, "") for l in _match(query)}))


@mcp.tool()
def stream_field_names(query: str = "*") -> str:
    """List the stream field names (labels identifying log streams) for logs matching the query."""
    return json.dumps(["service", "namespace", "pod"])


@mcp.tool()
def stream_field_values(field: str, query: str = "*") -> str:
    """List values of one stream field for logs matching the query."""
    return json.dumps(["checkout", "payments", "cart", "search", "auth"] if field == "service" else ["prod"])


@mcp.tool()
def streams(query: str = "*", limit: int = 50) -> str:
    """List log streams for logs matching the query."""
    return json.dumps([f'{{namespace="prod",service="{s}"}}' for s in ("checkout", "payments", "cart", "search", "auth")])


@mcp.tool()
def stream_ids(query: str = "*", limit: int = 50) -> str:
    """List log stream IDs for logs matching the query."""
    return json.dumps(["0000000000000001a2b3", "0000000000000001a2b4"])


@mcp.tool()
def stats_query(query: str, time: str | None = None) -> str:
    """Run a LogsQL stats query (e.g. count() by (service)) at a single point in time and return aggregated numbers."""
    m = _match(query)
    return json.dumps({s: sum(l["service"] == s for l in m) * 120 for s in {l["service"] for l in m}})


@mcp.tool()
def stats_query_range(query: str, start: str, end: str, step: str = "5m") -> str:
    """Run a LogsQL stats query over a time range and return aggregated numbers per step (log-based time series)."""
    return json.dumps({"payments": [["13:55", 2], ["14:00", 310], ["14:05", 402], ["14:10", 388], ["14:15", 120], ["14:20", 3]],
                       "checkout": [["13:55", 0], ["14:00", 150], ["14:05", 210], ["14:10", 190], ["14:15", 60], ["14:20", 0]]})


@mcp.tool()
def flags() -> str:
    """Show the non-default command-line flags of the VictoriaLogs instance (configuration, retention)."""
    return json.dumps({"retentionPeriod": "30d", "search.maxQueryDuration": "60s"})


@mcp.tool()
def documentation(query: str) -> str:
    """Search the embedded VictoriaLogs documentation (LogsQL syntax, functions, examples)."""
    return "LogsQL: `_time:1h service:payments level:error | stats count() by (service)`. Filters are space-separated (AND)."


if __name__ == "__main__":
    mcp.run()
