---
name: git_branch_delete
category: git
required_permission: repository:admin
tl_dr: Delete a git branch (DESTRUCTIVE).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    branch_name:
      type: string
      description: Branch name to delete
    confirmation_token:
      type: string
      description: Confirmation token (must be 'CONFIRM_DELETE_BRANCH')
  required:
  - repository_alias
  - branch_name
  - confirmation_token
  additionalProperties: false
outputSchema:
  oneOf:
  - type: object
    description: Success response after deletion
    properties:
      success:
        type: boolean
        description: Operation succeeded
      deleted_branch:
        type: string
        description: Name of deleted branch
  - type: object
    description: Confirmation token response
    properties:
      requires_confirmation:
        type: boolean
        description: Confirmation required
      token:
        type: string
        description: Confirmation token to use in next call
---

TL;DR: Delete a git branch (DESTRUCTIVE). USE CASES: (1) Delete merged feature branch, (2) Remove obsolete branch, (3) Clean up branches. SAFETY: Requires confirmation_token to prevent accidental deletion. Cannot delete current branch. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "branch_name": "old-feature", "confirmation_token": "CONFIRM_DELETE_BRANCH"} Returns: {"success": true, "deleted_branch": "old-feature"}