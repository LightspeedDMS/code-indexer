---
name: query_audit_logs
category: admin
required_permission: manage_users
tl_dr: Query security audit logs with optional filtering (admin only).
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