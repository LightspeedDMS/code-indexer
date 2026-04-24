---
name: git_reset
category: git
required_permission: repository:admin
tl_dr: Reset working tree to specific state (DESTRUCTIVE).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    mode:
      type: string
      enum:
      - soft
      - mixed
      - hard
      description: 'Reset mode: soft (keep staged), mixed (keep unstaged), hard (discard all)'
    commit_hash:
      type: string
      description: 'Optional commit hash to reset to (default: HEAD)'
    confirmation_token:
      type: string
      description: Confirmation token for destructive modes (required for hard reset)
  required:
  - repository_alias
  - mode
  additionalProperties: false
outputSchema:
  oneOf:
  - type: object
    description: Success response after reset performed
    properties:
      success:
        type: boolean
        description: Operation succeeded
      reset_mode:
        type: string
        description: Reset mode used (hard/mixed/soft)
      target_commit:
        type: string
        description: Commit reset to
  - type: object
    description: Confirmation token response for destructive operations
    properties:
      requires_confirmation:
        type: boolean
        description: Confirmation required
      token:
        type: string
        description: Confirmation token to use in next call
---

TL;DR: Reset working tree to specific state (DESTRUCTIVE). Reset working tree to specific state (DESTRUCTIVE). USE CASES: (1) Discard commits, (2) Reset to specific commit, (3) Clean working tree. MODES: soft (keep changes staged), mixed (keep changes unstaged), hard (discard all changes). SAFETY: Requires explicit mode. Optional commit_hash and confirmation_token for destructive operations. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123", "confirmation_token": "CONFIRM_RESET"} Returns: {"success": true, "reset_mode": "hard", "target_commit": "abc123"}