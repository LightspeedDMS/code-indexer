---
name: get_provider_health
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Get health metrics for embedding providers (latency, error rate, availability).'
slim_description: "[ADMIN ONLY] Get embedding provider health metrics including latency percentiles, error rate, and availability."
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Optional: specific provider name. Omit to get all providers."
  additionalProperties: false
---
[ADMIN ONLY] Get health metrics for configured embedding providers. Matches the REST provider-health routes, which require the admin role.

Returns `{"success": true, "provider_health": {"<provider>": {...}}}`. Each provider entry has status (healthy, degraded, down or sinbinned), health_score, p50_latency_ms, p95_latency_ms, p99_latency_ms, error_rate, availability, total_requests, successful_requests, failed_requests and window_minutes (the measurement window).

Errors: a caller without the admin role gets `{"success": false, "error": "Permission denied: admin role required"}`; other failures return `{"error": "<message>"}` with no `success` key.

Examples:
- All providers: `get_provider_health()`
- Specific: `get_provider_health(provider="voyage-ai")`
