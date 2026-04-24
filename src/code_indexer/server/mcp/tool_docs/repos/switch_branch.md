---
name: switch_branch
category: repos
required_permission: activate_repos
tl_dr: Switch YOUR activated repository to different branch and re-index automatically.
inputSchema:
  type: object
  properties:
    user_alias:
      type: string
      description: User alias of repository
    branch_name:
      type: string
      description: Target branch name
  required:
  - user_alias
  - branch_name
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Switch YOUR activated repository to different branch and re-index automatically. Changes the active branch for your user-specific repository copy. REQUIRES: Repository must be activated (use activate_repository first). QUICK START: switch_branch('my-backend', 'develop') switches to develop branch. USE CASES: (1) Work on different feature branches, (2) Compare code across branches (switch + search), (3) Test different versions. AUTOMATIC RE-INDEX: After branch switch, repository is automatically re-indexed to reflect new branch state. This ensures search results match current branch content. BRANCH DISCOVERY: Use get_branches or get_repository_status to list available branches before switching. WARNING: Uncommitted changes may be lost. Commit or stash changes before switching. ALIAS REQUIREMENT: Works only with YOUR activated repositories (user-specific aliases). Cannot switch branches on global read-only repositories. TROUBLESHOOTING: Branch not found? Use get_branches to verify branch exists. Repository not activated? Use activate_repository first. RELATED TOOLS: get_branches (list available branches), activate_repository (activate repo with specific branch), get_repository_status (check current branch), git_branch_create (create new branch), git_branch_switch (git operation alternative).