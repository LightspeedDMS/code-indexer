---
name: git_branch_delete
category: git
required_permission: repository:admin
tl_dr: Delete a git branch (DESTRUCTIVE).
slim_description: "Delete a git branch by name (DESTRUCTIVE)."
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
      description: Token returned by a previous git_branch_delete call for this repository and branch. Omit it on the first call.
  required:
  - repository_alias
  - branch_name
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
    required:
    - success
    - deleted_branch
  - type: object
    description: Confirmation required; nothing was deleted
    properties:
      success:
        type: boolean
        description: Always false
      confirmation_token_required:
        type: object
        description: Token to send back as confirmation_token
        properties:
          token:
            type: string
            description: Single-use token, valid for 5 minutes, for this user, repository and branch
          message:
            type: string
            description: What to do next, and why a presented token was rejected
        required:
        - token
        - message
    required:
    - success
    - confirmation_token_required
---

TL;DR: Delete a git branch (DESTRUCTIVE). USE CASES: (1) Delete merged feature branch, (2) Remove obsolete branch, (3) Clean up branches. SAFETY: Two-step confirmation. Call first without confirmation_token: nothing is deleted and the response carries confirmation_token_required.token. Call again with the same branch_name and that token to delete. The token is single-use, expires after 5 minutes, and is valid only for the same user, repository and branch; a missing, invalid or expired token returns a fresh one instead. Cannot delete current branch. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: first {"repository_alias": "my-repo", "branch_name": "old-feature"}, then {"repository_alias": "my-repo", "branch_name": "old-feature", "confirmation_token": "<token from the first response>"} Returns: {"success": true, "deleted_branch": "old-feature"}
