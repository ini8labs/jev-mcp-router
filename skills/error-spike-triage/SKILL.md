---
name: error-spike-triage
description: Playbook for triaging a spike in errors, 5xx responses or exceptions in a service.
---
1. Get the 5xx rate trend for the service (range query on http_requests_total{code=~"5.."}).
2. Count error logs per time bucket (hits) and find the dominant error message (query with level:error).
3. Check for deploys around the start of the spike (query logs for "deploy").
4. Report the start time, the dominant error and the suspected trigger.
