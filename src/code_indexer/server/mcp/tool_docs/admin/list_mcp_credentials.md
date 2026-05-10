---
name: list_mcp_credentials
category: admin
required_permission: query_repos
tl_dr: List MCP credentials by scope (self, user, all, system).
slim_description: "List MCP credentials. scope='self' lists own (no elevation); scope='user' lists by username (admin, elevation); scope='all' lists all users (admin, elevation); scope='system' lists system-managed (admin role + elevation)."
inputSchema:
  type: object
  properties:
    scope:
      type: string
      enum:
      - self
      - user
      - all
      - system
      description: "Scope: 'self' (own creds), 'user' (specific user, admin), 'all' (all users, admin), 'system' (system-managed, admin)"
    username:
      type: string
      description: "Required when scope='user'. The username to list credentials for."
  required:
  - scope
---

List MCP credentials by scope. Returns credential metadata (ID, description, created_at) but NOT the secret values.

SCOPE VARIANTS:
- scope='self': Lists caller's own credentials. No elevation required.
- scope='user': Lists a specific user's credentials (admin only). Requires elevation + username parameter.
- scope='all': Lists all users' credentials with username field on each entry (admin only). Requires elevation.
- scope='system': Lists system-managed credentials (admin role required). Requires elevation.

USE CASES:
- View your own MCP credentials: scope='self'
- Admin auditing a specific user: scope='user', username='alice'
- Security audit of all credentials: scope='all'
- View system-managed credentials: scope='system'

RETURNS (scope=self/user):
- credentials: Array of {id, description, created_at}

RETURNS (scope=all):
- credentials: Array of {id, username, description, created_at}

RETURNS (scope=system):
- system_credentials: Array of system credential objects
- count: Number of system credentials

EXAMPLES:
- List own: {"scope": "self"} -> {"success": true, "credentials": [...]}
- List user: {"scope": "user", "username": "alice"} -> {"success": true, "credentials": [...]}
- List all: {"scope": "all"} -> {"success": true, "credentials": [{..., "username": "alice"}, ...]}
- List system: {"scope": "system"} -> {"success": true, "system_credentials": [...], "count": 1}

NOTE: Full credential values are only shown once at creation time.
