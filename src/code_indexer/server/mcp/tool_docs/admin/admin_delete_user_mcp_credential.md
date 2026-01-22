---
name: admin_delete_user_mcp_credential
category: admin
required_permission: manage_users
tl_dr: Delete an MCP credential for a specific user (admin only).
---

Delete an MCP credential for a specific user (admin only). The credential will be immediately invalidated.

USE CASES:
- Admin revoking compromised credentials
- Admin removing credentials for deactivated users
- Security incident response

INPUTS:
- username (required): The username whose credential to delete
- credential_id (required): The unique identifier of the credential to delete

RETURNS:
- success: Boolean indicating if deletion succeeded

PERMISSIONS: Requires manage_users (admin only).