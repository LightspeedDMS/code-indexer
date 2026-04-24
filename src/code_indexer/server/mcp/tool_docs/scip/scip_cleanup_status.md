---
name: scip_cleanup_status
category: scip
required_permission: manage_users
tl_dr: Get SCIP workspace cleanup job status (admin only).
---

Get SCIP workspace cleanup job status (admin only). Returns the current status of the SCIP workspace cleanup operation.

USE CASES:
- Monitor progress of triggered cleanup job
- Check if cleanup is currently running
- Verify cleanup completion

RETURNS:
- running: Boolean indicating if cleanup is in progress
- job_id: Current cleanup job ID (if running)
- progress: Progress description (if running)
- last_cleanup_time: ISO timestamp of last completed cleanup
- workspace_count: Current number of SCIP workspaces

PERMISSIONS: Requires manage_users (admin only).