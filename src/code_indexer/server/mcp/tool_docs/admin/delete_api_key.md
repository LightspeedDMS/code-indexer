---
name: delete_api_key
category: admin
required_permission: query_repos
tl_dr: Delete an API key and immediately invalidate it.
---

TL;DR: Delete an API key and immediately invalidate it. Delete an API key belonging to the authenticated user. The key will be immediately invalidated.

USE CASES:
- Revoke compromised key
- Remove unused keys
- Rotate keys

INPUTS:
- key_id (required): The unique identifier of the key to delete

RETURNS:
- success: Boolean indicating if deletion succeeded

NOTE: You can only delete your own keys. Use list_api_keys to find key IDs.

EXAMPLE: {"key_id": "key_xyz"} Returns: {"success": true}