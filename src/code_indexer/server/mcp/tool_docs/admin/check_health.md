---
name: check_health
category: admin
required_permission: query_repos
tl_dr: Check CIDX server health and availability.
slim_description: "Check CIDX server health without parameters."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    server_version:
      type: string
      description: Server version string
    health:
      type: object
      description: System health information
      properties:
        status:
          type: string
          enum:
          - healthy
          - degraded
          - unhealthy
          description: Overall health status
        timestamp:
          type: string
          description: ISO 8601 health check timestamp
        checks:
          type: object
          description: Individual service health checks
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Check CIDX server health and availability. QUICK START: check_health() with no parameters returns health status. USE CASES: (1) Verify server is operational before operations, (2) Debug connection issues, (3) Monitor system availability. OUTPUT: `{"success": true, "server_version": "...", "node_id": "<cluster node id, or null in solo mode>", "health": {...}}`. `health` is the same report as the server's health endpoint: `status` (healthy/degraded/unhealthy), `timestamp`, `services` (`database` and `storage`, each with status, response_time_ms and error_message), `system` (memory, CPU, disk, network and swap metrics, active background jobs, mounted volumes), `failure_reasons` (up to three reasons when degraded or unhealthy) and further informational fields. TROUBLESHOOTING: `status` degraded or unhealthy lists its causes in `failure_reasons`. If the check itself fails, the response is `{"success": false, "error": "<message>", "health": {}}`. WHEN TO USE: Before starting work session to confirm server availability, or when experiencing unexpected errors. NO PARAMETERS REQUIRED: Health check needs no input arguments. RELATED TOOLS: repository_status (check specific repo health), get_all_repositories_status (all repos health), get_job_statistics (background job health).
