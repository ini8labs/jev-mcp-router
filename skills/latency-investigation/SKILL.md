---
name: latency-investigation
description: Playbook for investigating slow responses or high latency (p95/p99) of a service.
---
1. Pull the p99 latency trend for the service over the last hour (range query on http_request_duration_seconds_bucket).
2. Check downstream dependencies: DB pool saturation (db_pool_connections_in_use vs db_pool_connections_max).
3. Search error/timeout logs for the service and its dependencies in the same window.
4. Report: when it started, peak value, likely cause, whether it has recovered.
