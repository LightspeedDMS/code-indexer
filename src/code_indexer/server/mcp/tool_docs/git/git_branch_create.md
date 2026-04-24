---
name: git_branch_create
category: git
required_permission: repository:write
tl_dr: Create a new git branch at current HEAD.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    branch_name:
      type: string
      description: Name for new branch
  required:
  - repository_alias
  - branch_name
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    created_branch:
      type: string
      description: Name of newly created branch
---

TL;DR: Create a new git branch at current HEAD. Create a new git branch. USE CASES: (1) Create feature branch, (2) Create bugfix branch, (3) Isolate work in new branch. REQUIREMENTS: Branch name must be unique. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "branch_name": "feature/new-feature"} Returns: {"success": true, "created_branch": "feature/new-feature"}