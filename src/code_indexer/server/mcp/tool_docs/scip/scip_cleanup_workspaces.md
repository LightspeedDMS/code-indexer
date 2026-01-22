---
name: scip_cleanup_workspaces
category: scip
required_permission: manage_users
tl_dr: Trigger SCIP workspace cleanup job (admin only).
---

Trigger SCIP workspace cleanup job (admin only). Starts an async cleanup job to remove expired SCIP self-healing workspaces and reclaim disk space.

USE CASES:
- Manually trigger cleanup when disk space is low
- Clean up after failed SCIP operations
- Force workspace cleanup outside scheduled window

RETURNS:
- job_id: Unique identifier for the cleanup job
- status: Initial job status (typically 'started')

NOTE: This is an async operation. Use scip_cleanup_status to monitor progress.

PERMISSIONS: Requires manage_users (admin only).