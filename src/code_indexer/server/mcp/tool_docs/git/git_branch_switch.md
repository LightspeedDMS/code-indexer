---
name: git_branch_switch
category: git
required_permission: repository:write
tl_dr: Switch to a different branch (git checkout).
---

TL;DR: Switch to a different branch (git checkout). USE CASES: (1) Switch to existing branch, (2) Change working context, (3) Review different branch. REQUIREMENTS: Branch must exist, working tree must be clean. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "branch_name": "main"} Returns: {"success": true, "from_branch": "develop", "to_branch": "main"}