---
name: git_status
category: git
required_permission: repository:read
tl_dr: Get working tree status showing staged/unstaged/untracked files.
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
    staged:
      type: array
      items:
        type: string
      description: List of staged files
    unstaged:
      type: array
      items:
        type: string
      description: List of unstaged files
    untracked:
      type: array
      items:
        type: string
      description: List of untracked files
  required:
  - success
---

TL;DR: Get working tree status showing staged/unstaged/untracked files. Get git working tree status for an activated repository. USE CASES: (1) Check modified/staged/untracked files, (2) Verify working tree state before commits, (3) Identify conflicts. RETURNS: Staged files, unstaged changes, untracked files, current branch, merge conflicts. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "staged": ["src/main.py"], "unstaged": ["src/utils.py"], "untracked": ["new_file.py"]}