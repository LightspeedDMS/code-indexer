---
name: git_pull
category: git
required_permission: repository:write
tl_dr: Fetch and merge changes from remote repository.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    remote:
      type: string
      description: 'Remote name (default: origin)'
      default: origin
    branch:
      type: string
      description: 'Branch name (default: current branch)'
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    remote:
      type: string
      description: Remote name
    branch:
      type: string
      description: Branch name
    files_changed:
      type: integer
      description: Number of files changed
    commits_pulled:
      type: integer
      description: Number of new commits
---

TL;DR: Fetch and merge changes from remote repository. Pull changes from remote repository. USE CASES: (1) Fetch and merge remote changes, (2) Update local branch, (3) Sync with team changes. OPTIONAL: Specify remote (default: origin) and branch (default: current). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin", "branch": "main"} Returns: {"success": true, "remote": "origin", "branch": "main", "files_changed": 5, "commits_pulled": 2}