---
name: git_push
category: git
required_permission: repository:write
tl_dr: Push local commits to remote repository using your personal access token. Requires a git credential configured via configure_git_credential. Push uses HTTPS with PAT authentication and sets author/committer from your stored forge identity.
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
      description: Remote name (e.g., 'origin')
    branch:
      type: string
      description: Branch name pushed
    commits_pushed:
      type: integer
      description: Number of commits pushed
---

TL;DR: Push local commits to remote repository using your personal access token. Requires a git credential configured via configure_git_credential. Push uses HTTPS with PAT authentication and sets author/committer from your stored forge identity. USE CASES: (1) Push committed changes with correct identity attribution, (2) Sync local commits to GitHub/GitLab, (3) Share work with team using your PAT. REQUIRES: A credential configured via configure_git_credential for the repository's forge host (github.com, gitlab.com, etc.). OPTIONAL: Specify remote (default: origin) and branch (default: current). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin", "branch": "main"} Returns: {"success": true, "pushed_commits": 1}