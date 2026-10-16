---
name: git_amend
category: git
required_permission: repository:write
tl_dr: Amend the most recent git commit. Can update the commit message or just re-commit staged changes with the existing message. Keeps the original author; the committer identity comes from your stored git credential.
slim_description: "Amend the most recent commit, replacing its message if provided or keeping it unchanged when omitted (--no-edit)."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    message:
      type: string
      description: New commit message. If omitted, keeps the existing commit message (--no-edit).
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    commit_hash:
      type: string
      description: New commit hash after amending
    message:
      type: string
      description: Confirmation message with short commit hash
    error:
      type: string
      description: Error message on failure
    stderr:
      type: string
      description: Git stderr output on failure
---

TL;DR: Amend the most recent git commit. If a message is provided, replaces the commit message (git commit --amend -m). If no message is provided, keeps the existing message (git commit --amend --no-edit). Staged changes are folded into the amended commit.

IDENTITY: The original commit's author is kept. The committer comes from the git credential configured (configure_git_credential) for the forge host of the repository's origin remote: when that credential has an email, the committer email is that email and the committer name is the credential's name, or your username when it has no name. A credential without an email sets no identity, so the repository's git configuration supplies the committer. If no credential is configured for that host, the call fails with an error and nothing is amended.

USE CASES: (1) Fix a typo in the last commit message, (2) Add forgotten staged changes to the last commit without changing the message.

WORKFLOW: git_stage (stage additional files if needed) -> git_amend -> git_push.

WARNING: Amending rewrites the last commit and gives it a new hash. Amend only commits that have not been pushed yet: git_push performs a normal (non-force) push, so the remote rejects a push of an amended commit that replaces one already on the remote branch.

PERMISSIONS: Requires repository:write.

EXAMPLES: Fix message: {"repository_alias": "my-repo", "message": "Fix: corrected typo in feature implementation"} -> {"success": true, "commit_hash": "<new full hash>", "message": "Amended commit <short hash>"}. Keep message: {"repository_alias": "my-repo"} -> {"success": true, "commit_hash": "<new full hash>", "message": "Amended commit <short hash>"}
