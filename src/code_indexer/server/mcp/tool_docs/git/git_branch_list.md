---
name: git_branch_list
category: git
required_permission: repository:read
tl_dr: List all branches with current branch indicator.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    current:
      type: string
      description: Current branch name
    local:
      type: array
      items:
        type: string
      description: List of local branches
    remote:
      type: array
      items:
        type: string
      description: List of remote branches
  required:
  - success
  - current
  - local
  - remote
---

TL;DR: List all branches with current branch indicator. List all branches in repository. USE CASES: (1) View available branches, (2) Check current branch, (3) Identify remote branches. RETURNS: Local and remote branches with current branch indicator. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "current": "main", "local": ["main", "develop"], "remote": ["origin/main"]}