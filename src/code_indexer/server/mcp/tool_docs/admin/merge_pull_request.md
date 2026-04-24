---
name: merge_pull_request
category: admin
required_permission: repository:write
tl_dr: Merge a GitHub pull request or GitLab merge request.
---

TL;DR: Merge a GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Merge a feature branch PR after review approval, (2) Squash-merge a PR to keep a clean history, (3) Merge and automatically delete the source branch. MERGE METHODS: 'merge' (default) creates a merge commit, 'squash' squashes all commits into one, 'rebase' replays commits on top of the target branch. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "number": 42, "merge_method": "squash", "delete_branch": true} Returns: {"success": true, "merged": true, "sha": "abc123", "message": "PR #42 merged", "forge_type": "github"}
