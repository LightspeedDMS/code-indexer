---
name: scip_pr_history
category: scip
required_permission: manage_users
tl_dr: Get SCIP self-healing PR creation history (admin only).
---

Get SCIP self-healing PR creation history (admin only). Returns history of pull requests created by the SCIP self-healing system for dependency resolution and fix proposals.

USE CASES:
- Review SCIP self-healing activity
- Track automated dependency fix proposals
- Audit PR creation patterns

INPUTS:
- limit (optional): Maximum number of entries to return (default: 100)

RETURNS:
- history: Array of PR history entries with pr_number, repo, indexed_at, and status fields

PERMISSIONS: Requires manage_users (admin only).