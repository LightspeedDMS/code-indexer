---
name: git_pull
category: git
required_permission: repository:write
tl_dr: Fetch and merge changes from remote repository.
---

TL;DR: Fetch and merge changes from remote repository. Pull changes from remote repository. USE CASES: (1) Fetch and merge remote changes, (2) Update local branch, (3) Sync with team changes. OPTIONAL: Specify remote (default: origin) and branch (default: current). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin", "branch": "main"} Returns: {"success": true, "remote": "origin", "branch": "main", "files_changed": 5, "commits_pulled": 2}