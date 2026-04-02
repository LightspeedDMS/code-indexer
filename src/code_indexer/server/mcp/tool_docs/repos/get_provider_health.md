---
name: get_provider_health
category: repos
required_permission: query_repos
tl_dr: Get health metrics for embedding providers (latency, error rate, availability).
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Optional: specific provider name. Omit to get all providers."
  additionalProperties: false
---
Get health metrics for configured embedding providers.

Returns per-provider metrics: p50/p95/p99 latency, error rate, availability,
health score, and status (healthy/degraded/down).

Examples:
- All providers: `get_provider_health()`
- Specific: `get_provider_health(provider="voyage-ai")`
