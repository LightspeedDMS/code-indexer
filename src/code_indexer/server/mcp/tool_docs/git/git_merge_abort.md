---
name: git_merge_abort
category: git
required_permission: repository:write
tl_dr: Cancel in-progress merge and restore pre-merge state.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    message:
      type: string
      description: Confirmation message
---

TL;DR: Cancel in-progress merge and restore pre-merge state. Abort an in-progress merge operation. USE CASES: (1) Cancel merge with conflicts, (2) Restore pre-merge state, (3) Abandon merge attempt. REQUIREMENTS: Must have merge in progress. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "message": "Merge aborted"}