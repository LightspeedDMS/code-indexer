---
name: admin_create_user_mcp_credential
category: admin
required_permission: manage_users
tl_dr: Create a new MCP credential for a specific user (admin only).
---

Create a new MCP credential for a specific user (admin only). Returns the full credential (one-time display - provide it to the user immediately).

USE CASES:
- Admin provisioning credentials for new users
- Admin creating credentials on behalf of users

INPUTS:
- username (required): The username to create credential for
- description (optional): Human-readable label for the credential

RETURNS:
- credential_id: Unique identifier for the credential
- credential: Full credential value (provide to user - shown only once)

PERMISSIONS: Requires manage_users (admin only).