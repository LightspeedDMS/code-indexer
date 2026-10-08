---
name: git_reset
category: git
required_permission: repository:admin
tl_dr: Reset working tree to specific state (DESTRUCTIVE).
slim_description: "Reset the working tree to a specific state using soft, mixed, or hard mode; optionally targeting a commit_hash."
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
      description: Hard reset only. Token returned by a previous hard-reset call for this repository and commit. Omit it on the first call.
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
    required:
    - success
    - reset_mode
    - target_commit
  - type: object
    description: Hard reset confirmation required; nothing was reset
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
            description: Single-use token, valid for 5 minutes, for this user, repository and commit
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

TL;DR: Reset working tree to specific state (DESTRUCTIVE). USE CASES: (1) Discard commits, (2) Reset to specific commit, (3) Clean working tree. MODES: soft (keep changes staged), mixed (keep changes unstaged), hard (discard all changes). SAFETY: Requires explicit mode. A hard reset uses two-step confirmation: call first without confirmation_token and nothing is reset; the response carries confirmation_token_required.token. Call again with the same mode and commit_hash plus that token to reset. The token is single-use, expires after 5 minutes, and is valid only for the same user, repository and commit; a missing, invalid or expired token returns a fresh one instead. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: first {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123"}, then {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123", "confirmation_token": "<token from the first response>"} Returns: {"success": true, "reset_mode": "hard", "target_commit": "abc123"}
