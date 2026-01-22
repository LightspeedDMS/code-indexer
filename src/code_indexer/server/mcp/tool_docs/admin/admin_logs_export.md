---
name: admin_logs_export
category: admin
required_permission: manage_users
tl_dr: Export operational logs in JSON or CSV format for offline analysis or external
  tool import.
---

Export operational logs in JSON or CSV format for offline analysis or external tool import. USE CASES: (1) Download filtered logs for support tickets, (2) Import into Excel/log analysis tools, (3) Share error logs with team, (4) Archive logs. RETURNS: ALL logs matching filter criteria (no pagination) formatted as JSON or CSV. Includes export metadata with count and applied filters. PERMISSIONS: Requires admin role (admin only). EXAMPLE: {"format": "json", "search": "OAuth", "level": "ERROR"}