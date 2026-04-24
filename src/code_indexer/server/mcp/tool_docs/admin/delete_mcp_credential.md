---
name: delete_mcp_credential
category: admin
required_permission: query_repos
tl_dr: Delete an MCP credential belonging to the authenticated user.
---

Delete an MCP credential belonging to the authenticated user. The credential will be immediately invalidated.

USE CASES:
- Revoke compromised credential
- Remove unused credentials
- Rotate credentials

INPUTS:
- credential_id (required): The unique identifier of the credential to delete

RETURNS:
- success: Boolean indicating if deletion succeeded

NOTE: You can only delete your own credentials. Use list_mcp_credentials to find IDs.