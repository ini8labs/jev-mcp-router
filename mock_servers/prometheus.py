"""Mock of pab1it0/prometheus-mcp-server: same tool names and parameters,
canned data for one incident (checkout p99 latency spike caused by payments-db
connection pool exhaustion, 14:02-14:19 UTC). Swap for the real server in
mcp_servers.real.json."""

import json

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("prometheus")

SERVICES = ["checkout", "payments", "cart", "search", "auth"]
METRICS = {
    "http_requests_total": "counter: HTTP requests by service, route, code",
    "http_request_duration_seconds_bucket": "histogram: HTTP request latency by service, route",
    "process_cpu_seconds_total": "counter: CPU time used by the process",
    "process_resident_memory_bytes": "gauge: resident memory size",
    "db_pool_connections_in_use": "gauge: DB connections checked out of the pool",
    "db_pool_connections_max": "gauge: DB connection pool size",
    "kube_pod_container_status_restarts_total": "counter: container restarts",
    "up": "gauge: 1 if the scrape target is up",
}


def _svc(q: str) -> str:
    return next((s for s in SERVICES if s in q), "checkout")


def _series(q: str) -> dict:
    s, q = _svc(q), q.lower()
    if "duration" in q or "latency" in q or "quantile" in q:
        return {"metric": {"service": s}, "unit": "seconds",
                "points": [["13:50", 0.31], ["14:00", 0.33], ["14:05", 2.41], ["14:10", 2.86], ["14:15", 2.12], ["14:20", 0.36]]
                if s in ("checkout", "payments") else [["13:50", 0.12], ["14:10", 0.13], ["14:20", 0.12]]}
    if "pool" in q:
        return {"metric": {"service": "payments", "db": "payments-db"},
                "points": [["13:50", 18], ["14:00", 22], ["14:02", 50], ["14:10", 50], ["14:19", 31], ["14:20", 19]], "max": 50}
    if "5.." in q or 'code=~"5' in q or "error" in q:
        return {"metric": {"service": s, "code": "5xx"}, "unit": "req/s",
                "points": [["13:50", 0.1], ["14:05", 14.2], ["14:10", 19.8], ["14:20", 0.2]] if s in ("checkout", "payments") else [["14:10", 0.0]]}
    if "cpu" in q:
        return {"metric": {"service": s}, "unit": "cores", "points": [["13:50", 0.42], ["14:10", 0.47], ["14:20", 0.43]]}
    if "memory" in q:
        return {"metric": {"service": s}, "unit": "bytes", "points": [["13:50", 512e6], ["14:10", 530e6], ["14:20", 515e6]]}
    if "restart" in q:
        return {"metric": {"service": s}, "value": 0}
    return {"metric": {"service": s}, "unit": "req/s", "points": [["13:50", 220], ["14:10", 205], ["14:20", 224]]}


@mcp.tool()
def health_check() -> str:
    """Health check endpoint for container monitoring and status verification of the Prometheus MCP server."""
    return json.dumps({"status": "healthy", "prometheus": "reachable", "version": "2.53.0"})


@mcp.tool()
def execute_query(query: str, time: str | None = None) -> str:
    """Execute a PromQL instant query against Prometheus and return the current value of each matching series."""
    r = _series(query)
    pts = r.pop("points", None)
    return json.dumps({"resultType": "vector", "query": query, "result": [{**r, "value": pts[-1] if pts else r.get("value")}]})


@mcp.tool()
def execute_range_query(query: str, start: str, end: str, step: str) -> str:
    """Execute a PromQL range query with start time, end time, and step interval. Use for trends and how a metric changed over time."""
    return json.dumps({"resultType": "matrix", "query": query, "start": start, "end": end, "step": step,
                       "result": [_series(query)]})


@mcp.tool()
def list_metrics(limit: int = 100, offset: int = 0, filter_pattern: str | None = None) -> str:
    """List all available metric names in Prometheus with pagination and filtering support."""
    names = [m for m in METRICS if not filter_pattern or filter_pattern in m]
    return json.dumps({"metrics": names[offset : offset + limit], "total": len(names)})


@mcp.tool()
def get_metric_metadata(metric: str) -> str:
    """Get metadata (type, help text, unit) for a specific metric."""
    return json.dumps({metric: METRICS.get(metric, "unknown metric")})


@mcp.tool()
def get_targets(state: str | None = None, scrape_pool: str | None = None) -> str:
    """Get information about all scrape targets (which services Prometheus scrapes, their health and last scrape)."""
    targets = [{"job": s, "instance": f"{s}:9100", "health": "up", "lastScrape": "2s ago"} for s in SERVICES]
    return json.dumps({"activeTargets": [t for t in targets if not state or t["health"] == state]})


if __name__ == "__main__":
    mcp.run()
