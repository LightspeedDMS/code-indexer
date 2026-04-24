---
name: admin_list_user_mcp_credentials
category: admin
required_permission: manage_users
tl_dr: List all MCP credentials for a specific user (admin only).
inputSchema:
  type: object
  properties:
    username:
      type: string
      description: The username to list MCP credentials for
  required:
  - username
---

List all MCP credentials for a specific user (admin only). Returns credential metadata (ID, description, created_at) but NOT the secret values.

USE CASES:
- Admin auditing user credentials
- Admin helping user find credential IDs
- Security review of user access

INPUTS:
- username (required): The username to list credentials for

RETURNS:
- credentials: Array of credential metadata objects

PERMISSIONS: Requires manage_users (admin only).