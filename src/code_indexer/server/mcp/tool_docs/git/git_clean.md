---
name: git_clean
category: git
required_permission: repository:admin
tl_dr: Remove untracked files from working tree (DESTRUCTIVE).
slim_description: "Remove all untracked files from the working tree (DESTRUCTIVE)."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    confirmation_token:
      type: string
      description: Token returned by a previous git_clean call for this repository. Omit it on the first call.
  required:
  - repository_alias
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
    required:
    - success
    - removed_files
  - type: object
    description: Confirmation required; nothing was removed
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
            description: Single-use token, valid for 5 minutes, for this user and repository
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

TL;DR: Remove untracked files from working tree (DESTRUCTIVE). Runs `git clean -fd`: removes untracked files and untracked directories. Ignored files are kept. USE CASES: (1) Remove build artifacts, (2) Clean untracked files, (3) Restore clean state.

CONFIRMATION (two calls):
1. Call without confirmation_token (an empty string counts as none). Nothing is removed; the response carries confirmation_token_required.token.
2. Call again with that token. The clean runs and returns {"success": true, "removed_files": [...]}.
The token is single-use, expires after 5 minutes, and is valid only for the same user and repository; a missing, invalid or expired token returns a fresh one instead.

PERMISSIONS: Requires repository:admin (destructive operation).

EXAMPLE: first {"repository_alias": "my-repo"}, then {"repository_alias": "my-repo", "confirmation_token": "<token from the first response>"} Returns: {"success": true, "removed_files": ["build/"]}
