---
name: git_status
category: git
required_permission: repository:read
tl_dr: Get working tree status showing staged/unstaged/untracked files.
---

TL;DR: Get working tree status showing staged/unstaged/untracked files. Get git working tree status for an activated repository. USE CASES: (1) Check modified/staged/untracked files, (2) Verify working tree state before commits, (3) Identify conflicts. RETURNS: Staged files, unstaged changes, untracked files, current branch, merge conflicts. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "staged": ["src/main.py"], "unstaged": ["src/utils.py"], "untracked": ["new_file.py"]}