---
name: git_branch_list
category: git
required_permission: repository:read
tl_dr: List all branches with current branch indicator.
---

TL;DR: List all branches with current branch indicator. List all branches in repository. USE CASES: (1) View available branches, (2) Check current branch, (3) Identify remote branches. RETURNS: Local and remote branches with current branch indicator. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "current": "main", "local": ["main", "develop"], "remote": ["origin/main"]}