---
name: query_audit_logs
category: admin
required_permission: manage_users
tl_dr: Query security audit logs with optional filtering (admin only).
inputSchema:
  type: object
  properties:
    user:
      type: string
      description: Filter by username
    action:
      type: string
      description: Filter by action type (e.g., 'login', 'password_change', 'token_refresh')
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
  required: []
---

Query security audit logs with optional filtering (admin only). Returns audit log entries for authentication, authorization, and administrative actions.

USE CASES:
- Investigate security incidents
- Review user authentication history
- Audit administrative actions
- Monitor for suspicious activity

INPUTS:
- user (optional): Filter by username
- action (optional): Filter by action type (e.g., 'login', 'password_change')
- from_date (optional): Start date for time range (ISO 8601 format)
- to_date (optional): End date for time range (ISO 8601 format)
- limit (optional): Maximum number of entries to return (default: 100)

RETURNS:
- entries: Array of audit log entries with timestamp, user, action, resource, and details fields

PERMISSIONS: Requires manage_users (admin only).