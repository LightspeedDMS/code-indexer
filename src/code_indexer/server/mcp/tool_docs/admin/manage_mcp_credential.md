---
name: manage_mcp_credential
category: admin
required_permission: query_repos
tl_dr: Create or delete MCP credentials for self or another user.
slim_description: "Create or delete MCP credentials. action='create'|'delete'; omit target_user (or pass your own username) for self-service, provide another username for admin operations on that user. Elevation required when enforcement is on."
inputSchema:
  type: object
  properties:
    action:
      type: string
      enum:
      - create
      - delete
      description: "'create' to generate a new credential, 'delete' to revoke an existing one."
    credential_id:
      type: string
      description: "Required for action='delete'. The credential ID to revoke."
    description:
      type: string
      description: "Optional label for the credential (used with action='create')."
    target_user:
      type: string
      description: "Username to operate on. Omit, or pass your own username, for self-service; another username requires the admin role."
  required:
  - action
---

Create or delete MCP credentials. Every operation requires an active elevation window (TOTP step-up via `elevate_session`) when elevation enforcement is on.

OPERATION MATRIX:
| action   | target_user                   | Operation                                       |
|----------|-------------------------------|-------------------------------------------------|
| create   | omitted or caller's username  | Create a credential for the caller              |
| delete   | omitted or caller's username  | Delete one of the caller's credentials          |
| create   | another username              | Admin: create a credential for that user        |
| delete   | another username              | Admin: delete one of that user's credentials    |

The admin role is required only when `target_user` names another user.

PARAMETERS:
- action (required): 'create' or 'delete'
- credential_id (required for action='delete'): ID of credential to revoke
- description (optional): Human-readable label for the credential
- target_user (optional): Username for admin operations on another user

RETURNS (action='create'):
- credential_id: Unique identifier (save for future deletion)
- client_id: Client ID for MCP authentication
- client_secret: Full secret value (SAVE - shown only once). `credential` carries the same value.
- description: The label given

RETURNS (action='delete'):
- success: true when the credential was revoked, false otherwise

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Missing required parameter: action`
- `Unknown action: '<action>'. Valid: create, delete`
- `Missing required parameter: credential_id` (delete)
- `Permission denied: admin role required` (another user's credentials)
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)

Full credential value shown only at creation time — store it securely.

EXAMPLES:
- Create own: {"action": "create", "description": "Dev env"} -> {"success": true, "credential_id": "<id>", "client_id": "<client id>", "client_secret": "<secret>", "credential": "<secret>", "description": "Dev env"}
- Delete own: {"action": "delete", "credential_id": "<id>"} -> {"success": true}
- Admin create: {"action": "create", "target_user": "example-user", "description": "CI"} -> {"success": true, "credential_id": "<id>", ...}
- Admin delete: {"action": "delete", "target_user": "example-user", "credential_id": "<id>"} -> {"success": true}
