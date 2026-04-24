---
name: git_stash
category: git
required_permission: repository:write
tl_dr: Stash and restore uncommitted changes in a git repository. Supports push, pop, apply, list, and drop actions.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    action:
      type: string
      description: 'Stash action: push, pop, apply, list, or drop'
      enum:
      - push
      - pop
      - apply
      - list
      - drop
    message:
      type: string
      description: 'Optional stash message (only for push action)'
    index:
      type: integer
      description: 'Stash index to operate on (for pop/apply/drop, default: 0)'
      default: 0
  required:
  - repository_alias
  - action
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    stash_ref:
      type: string
      description: Stash reference created (e.g. stash@{0}), returned by push
    stashes:
      type: array
      description: List of stash entries, returned by list
      items:
        type: object
        properties:
          index:
            type: integer
          message:
            type: string
          created_at:
            type: string
    message:
      type: string
      description: Output message from git
    error:
      type: string
      description: Error message on failure
---

TL;DR: Stash and restore uncommitted changes. Supports five actions: push (save changes to stash), pop (apply and remove stash entry), apply (apply without removing), list (show all stash entries), drop (remove without applying). USE CASES: (1) Temporarily save work-in-progress before switching branches, (2) Apply stashed changes to a different branch, (3) List all saved stashes to find a specific one. ACTIONS: push=save changes, pop=restore+remove, apply=restore (keep stash), list=show all, drop=delete entry. PERMISSIONS: Requires repository:write. EXAMPLES: Push: {"repository_alias": "my-repo", "action": "push", "message": "WIP feature"} -> {"success": true, "stash_ref": "stash@{0}"}. List: {"repository_alias": "my-repo", "action": "list"} -> {"success": true, "stashes": [{"index": 0, "message": "WIP feature", "created_at": "..."}]}. Pop: {"repository_alias": "my-repo", "action": "pop", "index": 0} -> {"success": true}
