---
name: delete_git_credential
category: admin
required_permission: query_repos
tl_dr: Delete a git forge credential.
inputSchema:
  type: object
  properties:
    credential_id:
      type: string
      description: The credential ID to delete (from list_git_credentials)
  required: [credential_id]
---

TL;DR: Delete a previously configured git forge credential. You can only delete your own credentials.

USE CASES:
- Remove a credential when the PAT has been revoked
- Clean up unused forge configurations

INPUTS:
- credential_id (required): The credential ID to delete

RETURNS:
- success: true if deleted

SECURITY: Ownership is enforced - you cannot delete another user's credentials.

EXAMPLE: {"credential_id": "uuid"} Returns: {"success": true, "message": "Credential uuid deleted"}
