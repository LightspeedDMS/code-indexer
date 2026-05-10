---
name: manage_mcp_credential
category: admin
required_permission: query_repos
tl_dr: Create or delete MCP credentials for self or another user.
slim_description: "Create or delete MCP credentials. action='create'|'delete'; omit target_user for self-service (elevation required), provide it for admin operations on another user (elevation required)."
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
      description: "Username to operate on (admin only). Omit for self-service."
  required:
  - action
---

Create or delete MCP credentials. All operations require step-up elevation.

OPERATION MATRIX:
| action   | target_user | Operation                              |
|----------|-------------|----------------------------------------|
| create   | (omitted)   | Create credential for caller           |
| delete   | (omitted)   | Delete caller's credential             |
| create   | "alice"     | Admin: create credential for alice     |
| delete   | "alice"     | Admin: delete alice's credential       |

PARAMETERS:
- action (required): 'create' or 'delete'
- credential_id (required for action='delete'): ID of credential to revoke
- description (optional): Human-readable label for the credential
- target_user (optional): Username for admin operations on another user

RETURNS (action='create'):
- credential_id: Unique identifier (save for future deletion)
- credential/client_secret: Full secret value (SAVE - shown only once)
- client_id: Client ID for MCP authentication

RETURNS (action='delete'):
- success: true/false

SECURITY: Step-up TOTP elevation is required for all operations.
Full credential value shown only at creation time — store it securely.

EXAMPLES:
- Create own: {"action": "create", "description": "Dev env"} -> {"success": true, "credential_id": "cred_abc", "credential": "mcp_sk_xyz..."}
- Delete own: {"action": "delete", "credential_id": "cred_abc"} -> {"success": true}
- Admin create: {"action": "create", "target_user": "alice", "description": "CI"} -> {"success": true, "credential_id": "cred_xyz", ...}
- Admin delete: {"action": "delete", "target_user": "alice", "credential_id": "cred_xyz"} -> {"success": true}
