---
name: git_branch_create
category: git
required_permission: repository:write
tl_dr: Create a new git branch at current HEAD.
---

TL;DR: Create a new git branch at current HEAD. Create a new git branch. USE CASES: (1) Create feature branch, (2) Create bugfix branch, (3) Isolate work in new branch. REQUIREMENTS: Branch name must be unique. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "branch_name": "feature/new-feature"} Returns: {"success": true, "created_branch": "feature/new-feature"}