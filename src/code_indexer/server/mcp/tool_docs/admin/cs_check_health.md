---
name: cs_check_health
category: admin
required_permission: delegate_open
tl_dr: Check Claude Server connectivity and health status.
inputSchema:
  type: object
  properties: {}
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: True if health check succeeded
    health:
      type: object
      description: Health data from Claude Server
      properties:
        status:
          type: string
          description: Overall health status (e.g. healthy, degraded, unhealthy)
        nodeId:
          type: string
          description: Unique identifier of the Claude Server node
        version:
          type: string
          description: Claude Server version string
        checks:
          type: object
          description: Component health checks (database, storage, queueService)
        metrics:
          type: object
          description: Server metrics (queueDepth, runningJobs)
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
---

Check Claude Server connectivity and health status. Returns component health checks and server metrics.

REQUIRED PERMISSION: delegate_open (power_user or admin role)

BEHAVIOR:
Calls GET /health on Claude Server. The health endpoint on Claude Server is anonymous, but access is gated at the CIDX level with the delegate_open permission. Returns status, nodeId, version, component checks (database, storage, queueService), and metrics (queueDepth, runningJobs).

USE CASE: Use this tool to verify that Claude Server is reachable and all components are healthy before submitting delegation jobs. If this tool returns an error, delegation jobs will likely fail.

ERRORS:
- 'Claude Delegation not configured' -> Delegation configuration not set up
- 'Access denied' -> User does not have delegate_open permission
- 'Failed to check health' -> Claude Server is unreachable or returned an error
