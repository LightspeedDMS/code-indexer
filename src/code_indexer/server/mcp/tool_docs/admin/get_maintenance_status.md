---
name: get_maintenance_status
category: admin
required_permission: query_repos
tl_dr: Get current server maintenance mode status.
inputSchema:
  type: object
  properties: {}
  required: []
---

Get current server maintenance mode status. Returns whether the server is in maintenance mode and job statistics.

USE CASES:
- Check if server is in maintenance mode before starting operations
- Monitor drain progress during maintenance
- Verify maintenance mode state

RETURNS:
- in_maintenance: Boolean indicating if server is in maintenance mode
- message: Maintenance message (if in maintenance mode)
- since: ISO timestamp when maintenance started (if applicable)
- drained: Boolean indicating if all jobs have completed
- running_jobs: Number of jobs still running
- queued_jobs: Number of jobs in queue

PERMISSIONS: Requires query_repos (any authenticated user can check status).