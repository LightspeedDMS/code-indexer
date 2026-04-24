---
name: enter_maintenance_mode
category: admin
required_permission: manage_users
tl_dr: Enter server maintenance mode (admin only).
inputSchema:
  type: object
  properties:
    message:
      type: string
      description: Optional custom maintenance message
  required: []
---

Enter server maintenance mode (admin only). Stops accepting new background jobs while allowing running jobs to complete. Query endpoints remain available during maintenance.

USE CASES:
- Prepare for server updates or deployments
- Gracefully drain active jobs before restart
- Coordinate with auto-update systems

INPUTS:
- message (optional): Custom maintenance message to display

RETURNS:
- success: Boolean indicating operation result
- message: Status message with job counts
- maintenance_mode: Current maintenance mode state
- running_jobs: Number of jobs still running
- queued_jobs: Number of jobs in queue

PERMISSIONS: Requires manage_users (admin only).