---
name: list_mcp_credentials
category: admin
required_permission: query_repos
tl_dr: List all MCP credentials for the authenticated user.
---

TL;DR: List all MCP credentials for the authenticated user. Returns credential metadata (ID, description, created_at) but NOT the secret values.

USE CASES:
- View your existing MCP credentials
- Find credential ID for deletion

RETURNS:
- credentials: Array of credential metadata objects

NOTE: Full credential values are only shown once at creation time.

EXAMPLE: {} Returns: {"success": true, "credentials": [{"id": "cred_abc", "description": "Dev env", "created_at": "2024-01-15T10:00:00Z"}]}