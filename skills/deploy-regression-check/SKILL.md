---
name: deploy-regression-check
description: Playbook for checking whether a recent deployment or release caused a regression.
---
1. Find deploy events in logs (query "deploy") and note their times.
2. Compare error rate and p99 latency 15 minutes before vs after each deploy.
3. Report which deploy, if any, correlates with the regression, and whether it was rolled back.
