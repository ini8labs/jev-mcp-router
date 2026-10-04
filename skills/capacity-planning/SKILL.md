---
name: capacity-planning
description: Playbook for capacity planning - forecasting CPU, memory and traffic growth to size infrastructure.
---
1. Pull 30-day CPU and memory trends per service.
2. Pull request-rate growth per service.
3. Project 90 days ahead and flag services above 70% of their limits.
