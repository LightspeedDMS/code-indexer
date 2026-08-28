---
name: query_audit_logs
category: admin
required_permission: manage_users
tl_dr: Query security audit logs with optional filtering (admin only).
slim_description: "Query security audit logs with optional filters for user, action, date range, and limit."
inputSchema:
  type: object
  properties:
    user:
      type: string
      description: Filter by username
    action:
      type: string
      description: "Filter by exact action_type (exact, case-sensitive match; e.g. 'password_change_success', 'token_refresh_failure', 'pr_creation_success', 'git_cleanup', 'group_create'). Prefixes such as 'password_change' or 'pr_creation' do NOT match."
    from_date:
      type: string
      description: Start date for time range filter (ISO 8601 format, e.g., '2024-01-01')
    to_date:
      type: string
      description: End date for time range filter (ISO 8601 format, e.g., '2024-12-31')
    limit:
      type: integer
      description: Maximum number of entries to return
      default: 100
      minimum: 1
      maximum: 1000
    page:
      type: integer
      description: Page number for pagination (1-based)
      default: 1
      minimum: 1
  required: []
---

Query security audit logs with optional filtering (admin only). Requires MCP elevation (TOTP step-up). Returns audit log entries for authentication, authorization, and administrative actions.

USE CASES:
- Investigate security incidents
- Review user authentication history
- Audit administrative actions
- Monitor for suspicious activity

INPUTS:
- user (optional): Filter by username
- action (optional): Filter by exact action_type (exact, case-sensitive match; e.g. 'password_change_success', 'token_refresh_failure', 'pr_creation_success', 'git_cleanup', 'group_create'). Prefixes such as 'password_change' or 'pr_creation' do NOT match.
- from_date (optional): Start date for time range (ISO 8601 format)
- to_date (optional): End date for time range (ISO 8601 format)
- limit (optional): Maximum number of entries to return (default: 100)
- page (optional): Page number for pagination (1-based, default: 1)

RETURNS:
- entries: Array of audit log entries with timestamp, user, action, resource, and details fields
- total: True count of all matching entries across every page (not just len(entries))

PERMISSIONS: Requires manage_users (admin only).

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)