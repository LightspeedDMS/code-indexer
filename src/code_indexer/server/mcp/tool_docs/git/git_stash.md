---
name: git_stash
category: git
required_permission: repository:write
tl_dr: Stash and restore uncommitted changes.
---

TL;DR: Stash and restore uncommitted changes. Supports five actions: push (save changes to stash), pop (apply and remove stash entry), apply (apply without removing), list (show all stash entries), drop (remove without applying). USE CASES: (1) Temporarily save work-in-progress before switching branches, (2) Apply stashed changes to a different branch, (3) List all saved stashes to find a specific one. ACTIONS: push=save changes, pop=restore+remove, apply=restore (keep stash), list=show all, drop=delete entry. PERMISSIONS: Requires repository:write. EXAMPLES: Push: {"repository_alias": "my-repo", "action": "push", "message": "WIP feature"} -> {"success": true, "stash_ref": "stash@{0}"}. List: {"repository_alias": "my-repo", "action": "list"} -> {"success": true, "stashes": [{"index": 0, "message": "WIP feature", "created_at": "..."}]}. Pop: {"repository_alias": "my-repo", "action": "pop", "index": 0} -> {"success": true}
