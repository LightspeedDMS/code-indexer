---
name: switch_branch
category: repos
required_permission: activate_repos
tl_dr: Switch YOUR activated repository to different branch and re-index automatically.
slim_description: "Switch a user-activated repository to a different branch by user_alias and branch_name, with automatic re-indexing."
inputSchema:
  type: object
  properties:
    user_alias:
      type: string
      description: User alias of repository
    branch_name:
      type: string
      description: Target branch name
    create:
      type: boolean
      description: 'If true, create branch_name if it does not already exist (default: false).'
      default: false
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

TL;DR: Switch YOUR activated repository to a different branch. Changes the active branch for your user-specific repository copy. REQUIRES: Repository must be activated (use activate_repository first). QUICK START: switch_branch(user_alias='my-work', branch_name='develop') switches to the develop branch. Set create=true to create branch_name if it does not already exist. USE CASES: (1) Work on different feature branches, (2) Compare code across branches (switch + search), (3) Test different versions. RE-INDEX: The switch runs within the call (no background job, no job_id) and returns `{"success": true, "message": "..."}`. When the target branch differs from the golden repository's default branch, a branch delta re-index also runs within the call before it returns; switching to the default branch runs no re-index. A failed switch or re-index returns `{"success": false, "error": "..."}`. BRANCH DISCOVERY: Use get_branches or repository_status to list available branches before switching. WARNING: Uncommitted changes may be lost. Commit or stash changes before switching. ALIAS REQUIREMENT: Works only with YOUR activated repositories (user-specific aliases). Cannot switch branches on global read-only repositories. TROUBLESHOOTING: Branch not found? Use get_branches to verify branch exists. Repository not activated? Use activate_repository first. RELATED TOOLS: get_branches (list available branches), activate_repository (activate repo with specific branch), repository_status (check current branch), git_branch_create (create new branch), git_branch_switch (git operation alternative).
