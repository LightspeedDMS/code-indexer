---
name: get_provider_health
category: admin
required_permission: query_repos
tl_dr: 'Get health metrics for configured embedding providers.


  Returns per-provider metrics: p50/p95/p99 latency, error rate, availability,

  health score, and status (healthy/degraded/down).


  Examples:

  - All providers: `get_provider_health()`

  - Specific: `get_provider_health(provider="voyage-ai")`.'
---

Get health metrics for configured embedding providers.

Returns per-provider metrics: p50/p95/p99 latency, error rate, availability,
health score, and status (healthy/degraded/down).

Examples:
- All providers: `get_provider_health()`
- Specific: `get_provider_health(provider="voyage-ai")`
