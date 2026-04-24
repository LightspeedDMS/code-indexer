---
name: git_branch_switch
category: git
required_permission: repository:write
tl_dr: Switch to a different branch (git checkout).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    branch_name:
      type: string
      description: Branch name to switch to
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
    from_branch:
      type: string
      description: Previous branch
    to_branch:
      type: string
      description: New current branch
---

TL;DR: Switch to a different branch (git checkout). USE CASES: (1) Switch to existing branch, (2) Change working context, (3) Review different branch. REQUIREMENTS: Branch must exist, working tree must be clean. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "branch_name": "main"} Returns: {"success": true, "from_branch": "develop", "to_branch": "main"}