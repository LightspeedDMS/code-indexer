---
name: admin_list_all_mcp_credentials
category: admin
required_permission: manage_users
tl_dr: List all MCP credentials across all users (admin only).
inputSchema:
  type: object
  properties: {}
  required: []
---

List all MCP credentials across all users (admin only). Returns credential metadata with username for each credential.

USE CASES:
- Admin auditing all credentials in the system
- Security review of credential usage
- Identifying orphaned or suspicious credentials

RETURNS:
- credentials: Array of credential metadata objects, each with username field

PERMISSIONS: Requires manage_users (admin only).