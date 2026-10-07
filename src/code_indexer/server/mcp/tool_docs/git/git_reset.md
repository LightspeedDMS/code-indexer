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
      description: 'Only used with mode=hard. Omit it on the first call to receive a generated token, then pass that
        token on the second call. Tokens are single-use and expire after 5 minutes. Ignored for soft and mixed.'
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

TL;DR: Reset working tree to specific state (DESTRUCTIVE). Runs `git reset --<mode> <commit_hash>` (commit_hash defaults to HEAD). USE CASES: (1) Discard commits, (2) Reset to specific commit, (3) Discard uncommitted changes (hard). MODES: soft (keep changes staged), mixed (keep changes unstaged), hard (discard all changes). Pass mode explicitly.

HARD RESET CONFIRMATION (two calls): soft and mixed run immediately. mode=hard needs a confirmation token that the server generates:
1. Call without confirmation_token. Nothing is reset; the response is {"success": false, "confirmation_token_required": {"token": "<6-character code>", "message": "Hard reset requires confirmation. Call again with confirmation_token='<code>'"}}.
2. Call again with the same arguments plus confirmation_token set to that code. The reset runs.
Tokens are single-use and expire after 5 minutes. An invalid or expired token runs nothing; call again without a token to get a new one.

PERMISSIONS: Requires repository:admin (destructive operation).

EXAMPLE (hard reset): first {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123"} returns a token such as "K7M2QX"; then {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123", "confirmation_token": "K7M2QX"} returns {"success": true, "reset_mode": "hard", "target_commit": "abc123"}
