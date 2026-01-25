---
name: git_clean
category: git
required_permission: repository:admin
tl_dr: Remove untracked files from working tree (DESTRUCTIVE).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    confirmation_token:
      type: string
      description: Confirmation token (must be 'CONFIRM_DELETE_UNTRACKED')
  required:
  - repository_alias
  - confirmation_token
  additionalProperties: false
outputSchema:
  oneOf:
  - type: object
    description: Success response after clean performed
    properties:
      success:
        type: boolean
        description: Operation succeeded
      removed_files:
        type: array
        items:
          type: string
        description: List of untracked files/directories removed
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

Remove untracked files from working tree (DESTRUCTIVE). USE CASES: (1) Remove build artifacts, (2) Clean untracked files, (3) Restore clean state. SAFETY: Requires confirmation_token to prevent accidental deletion. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "confirmation_token": "CONFIRM_DELETE_UNTRACKED"}