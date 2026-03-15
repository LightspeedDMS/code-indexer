---
name: close_pull_request
category: git
required_permission: repository:write
tl_dr: Close (without merging) a GitHub pull request or GitLab merge request. Auto-detects forge type from the remote URL. Credentials are auto-fetched from stored git credentials.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: Pull/merge request number to close
  required:
  - repository_alias
  - number
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
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Close a GitHub pull request or GitLab merge request without merging it. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Close a stale PR that will not be merged, (2) Decline a contribution PR, (3) Close a PR in favor of a different approach. NOTE: This closes the PR without merging. To merge, use merge_pull_request instead. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "number": 42} Returns: {"success": true, "message": "PR #42 closed", "forge_type": "github"}
