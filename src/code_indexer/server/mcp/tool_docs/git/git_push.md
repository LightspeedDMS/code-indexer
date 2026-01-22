---
name: git_push
category: git
required_permission: repository:write
tl_dr: Push local commits to remote repository.
---

TL;DR: Push local commits to remote repository. Push commits to remote repository. USE CASES: (1) Push committed changes, (2) Sync local commits to remote, (3) Share work with team. OPTIONAL: Specify remote (default: origin) and branch (default: current). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin", "branch": "main"} Returns: {"success": true, "remote": "origin", "branch": "main", "commits_pushed": 3}