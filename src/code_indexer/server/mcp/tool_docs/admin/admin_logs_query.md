---
name: admin_logs_query
category: admin
required_permission: manage_users
tl_dr: Query operational logs from SQLite database with pagination and filtering.
---

Query operational logs from SQLite database with pagination and filtering. USE CASES: (1) View recent server logs, (2) Search for specific errors/events, (3) Trace requests by correlation_id, (4) Filter by log level. RETURNS: Paginated array of log entries with timestamp, level, source, message, correlation_id, user_id, request_path. PERMISSIONS: Requires admin role (admin only). EXAMPLE: {"page": 1, "page_size": 50, "search": "SSO", "level": "ERROR"}