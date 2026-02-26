---
name: admin_list_system_mcp_credentials
category: admin
required_permission: manage_users
tl_dr: List system-managed MCP credentials owned by the admin user (admin only).
inputSchema:
  type: object
  properties: {}
  required: []
---

List MCP credentials that are owned by the built-in 'admin' user and were created
automatically by the CIDX server (e.g. cidx-local-auto, cidx-server-auto).

USE CASES:
- Admin auditing which system credentials exist and when they were last used
- Troubleshooting MCPB or automation that relies on system-managed credentials
- Security review to verify no unexpected system credentials were created

RETURNS:
- system_credentials: Array of credential metadata objects, each with is_system=true
  and owner='admin (system)'
- count: Total number of system credentials returned

PERMISSIONS: Requires manage_users (admin only).
