---
name: close_pull_request
category: admin
required_permission: repository:write
tl_dr: Close a GitHub pull request or GitLab merge request without merging it.
---

TL;DR: Close a GitHub pull request or GitLab merge request without merging it. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Close a stale PR that will not be merged, (2) Decline a contribution PR, (3) Close a PR in favor of a different approach. NOTE: This closes the PR without merging. To merge, use merge_pull_request instead. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "number": 42} Returns: {"success": true, "message": "PR #42 closed", "forge_type": "github"}
