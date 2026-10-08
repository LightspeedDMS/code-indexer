---
name: delete_git_credential
category: admin
required_permission: query_repos
tl_dr: Delete a git forge credential.
slim_description: "Delete a previously configured git forge credential by credential_id, immediately removing it."
inputSchema:
  type: object
  properties:
    credential_id:
      type: string
      description: The credential ID to delete (from list_git_credentials)
  required: [credential_id]
---

TL;DR: Delete a previously configured git forge credential. Requires MCP elevation (TOTP step-up) when elevation enforcement is on. You can only delete your own credentials.

USE CASES:
- Remove a credential when the PAT has been revoked
- Clean up unused forge configurations

INPUTS:
- credential_id (required): The credential ID to delete

RETURNS:
- success: true if deleted

SECURITY: Ownership is enforced - you cannot delete another user's credentials.

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

EXAMPLE: {"credential_id": "uuid"} Returns: {"success": true, "message": "Credential uuid deleted"}
