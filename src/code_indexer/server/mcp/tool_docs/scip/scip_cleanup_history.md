---
name: scip_cleanup_history
category: scip
required_permission: manage_users
tl_dr: Get SCIP workspace cleanup history (admin only).
---

Get SCIP workspace cleanup history (admin only). Returns history of workspace cleanup operations performed by the SCIP self-healing system.

USE CASES:
- Review cleanup operation history
- Monitor disk space reclamation
- Audit workspace lifecycle management

INPUTS:
- limit (optional): Maximum number of entries to return (default: 100)

RETURNS:
- history: Array of cleanup history entries with cleanup_id, started_at, completed_at, and workspaces_cleaned fields

PERMISSIONS: Requires manage_users (admin only).