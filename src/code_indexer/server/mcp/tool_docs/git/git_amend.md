---
name: git_amend
category: git
required_permission: repository:write
tl_dr: Amend the most recent git commit. Can update the commit message or just re-commit staged changes with the existing message. Uses PAT credential identity for author/committer attribution.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias (must be in write mode or an activated workspace)
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

TL;DR: Amend the most recent git commit. If a message is provided, replaces the commit message. If no message is provided, keeps the existing message (equivalent to git commit --amend --no-edit). Author and committer identity are taken from stored PAT credentials. USE CASES: (1) Fix a typo in the last commit message, (2) Add forgotten staged changes to the last commit without changing the message, (3) Update author metadata on the last commit. WORKFLOW: git_stage (stage additional files if needed) -> git_amend -> git_push (with --force if already pushed). WARNING: Amending rewrites history. If the commit was already pushed to a shared branch, a force push will be needed. PERMISSIONS: Requires repository:write. EXAMPLES: Fix message: {"repository_alias": "my-repo", "message": "Fix: corrected typo in feature implementation"} -> {"success": true, "commit_hash": "abc123def"}. Keep message: {"repository_alias": "my-repo"} -> {"success": true, "commit_hash": "newhash456"}
