---
name: cidx_ssh_key_delete
category: ssh
required_permission: activate_repos
tl_dr: Delete CIDX-managed SSH key and remove from SSH config.
---

TL;DR: Delete CIDX-managed SSH key and remove from SSH config. WHEN TO USE: (1) Remove unused key, (2) Rotate compromised key, (3) Clean up old keys. WHEN NOT TO USE: Key is actively used by repositories -> reassign hosts first. SECURITY WARNING: Deletes both private and public key files from ~/.ssh/. Removes all Host entries from SSH config that use this key. Operation is IDEMPOTENT (always succeeds even if key doesn't exist). DESTRUCTIVE: Cannot be undone. RELATED TOOLS: cidx_ssh_key_list (view keys before deletion), cidx_ssh_key_create (create replacement key). EXAMPLE: {"name": "old-key"} Returns: {"success": true, "message": "Key old-key deleted successfully"}