---
name: git_push
category: git
required_permission: repository:write
tl_dr: Push local commits to remote repository using your personal access token. Requires a git credential configured via configure_git_credential. Push uses HTTPS with PAT authentication.
slim_description: "Push local commits to a remote repository using a stored PAT credential; requires configure_git_credential to be set up first."
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
      description: 'Destination branch the current HEAD is pushed to (default: the current branch)'
    set_upstream:
      type: boolean
      description: 'Push with git --set-upstream so the CURRENT branch tracks <remote>/<branch>, the destination branch pushed to (default: true). Set to false to leave tracking unchanged.'
      default: true
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
    pushed_commits:
      type: integer
      description: Number of commits pushed
---

TL;DR: Push local commits to remote repository using your personal access token. Requires a git credential configured via configure_git_credential. Push uses HTTPS with PAT authentication. USE CASES: (1) Push committed changes to your forge, (2) Sync local commits to GitHub/GitLab, (3) Share work with team using your PAT. REQUIRES: A credential configured via configure_git_credential for the repository's forge host (github.com, gitlab.com, etc.). OPTIONAL: Specify remote (default: origin) and branch (default: current). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin", "branch": "main"} Returns: {"success": true, "pushed_commits": 1}
