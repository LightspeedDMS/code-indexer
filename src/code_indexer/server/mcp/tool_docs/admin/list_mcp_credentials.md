---
name: list_mcp_credentials
category: admin
required_permission: query_repos
tl_dr: List MCP credentials by scope (self, user, all, system).
slim_description: "List MCP credentials. scope='self' lists own (no elevation); scope='user' lists by username (admin, elevation; your own username is served like scope='self'); scope='all' lists all users (admin, elevation); scope='system' lists system-managed (admin role + elevation)."
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
      description: "Scope: 'self' (own creds), 'user' (specific user, admin; your own username is served like 'self'), 'all' (all users, admin), 'system' (system-managed, admin)"
    username:
      type: string
      description: "Required when scope='user'. The username to list credentials for."
  required:
  - scope
---

List MCP credentials by scope. Returns credential metadata (ID, description, created_at) but NOT the secret values.

SCOPE VARIANTS:
- scope='self': Lists the caller's own credentials. No elevation required.
- scope='user': Lists the credentials of `username` (required). When `username` is the caller's own username, the request is served exactly like scope='self'. For any other username it requires the admin role and, when elevation enforcement is on, an active elevation window.
- scope='all': Lists every user's credentials with a username field on each entry. Admin role and elevation (when enforcement is on) required.
- scope='system': Lists system-managed credentials. Admin role and elevation (when enforcement is on) required.

USE CASES:
- View your own MCP credentials: scope='self'
- Admin auditing a specific user: scope='user', username='example-user'
- Audit of all credentials: scope='all'
- View system-managed credentials: scope='system'

RETURNS (scope=self/user):
- credentials: Array of {id, description, created_at}

RETURNS (scope=all):
- credentials: Array of {id, username, description, created_at}

RETURNS (scope=system):
- system_credentials: Array of system credential objects
- count: Number of system credentials

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Missing required parameter: scope`
- `Missing required parameter: username (required when scope='user')`
- `Unknown scope: '<scope>'. Valid: self, user, all, system`
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)

EXAMPLES:
- List own: {"scope": "self"} -> {"success": true, "credentials": [...]}
- List user: {"scope": "user", "username": "example-user"} -> {"success": true, "credentials": [...]}
- List all: {"scope": "all"} -> {"success": true, "credentials": [{..., "username": "example-user"}, ...]}
- List system: {"scope": "system"} -> {"success": true, "system_credentials": [...], "count": 1}

NOTE: Full credential values are only shown once at creation time.
