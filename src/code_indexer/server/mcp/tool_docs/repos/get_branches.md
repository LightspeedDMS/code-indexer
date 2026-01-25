---
name: get_branches
category: repos
required_permission: query_repos
tl_dr: List all git branches for repository with detailed metadata (current branch, last commit info, index status, remote
  tracking).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    branches:
      type: array
      description: List of branches
      items:
        type: object
        properties:
          name:
            type: string
            description: Branch name
          is_current:
            type: boolean
            description: Whether this is the active branch
          last_commit:
            type: object
            properties:
              sha:
                type: string
                description: Commit SHA
              message:
                type: string
                description: Commit message
              author:
                type: string
                description: Commit author
              date:
                type: string
                description: Commit date
          index_status:
            type:
            - object
            - 'null'
            description: Index status for this branch (nullable)
          remote_tracking:
            type:
            - object
            - 'null'
            description: Remote tracking information (nullable)
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: List all git branches for repository with detailed metadata (current branch, last commit info, index status, remote tracking). Supports both global and activated repositories. QUICK START: get_branches('backend-global') lists all branches. OUTPUT FIELDS: Each branch includes name, is_current (boolean), last_commit (sha, message, author, date), index_status (indexing state), remote_tracking (upstream branch info). USE CASES: (1) Discover available branches before switch_branch, (2) Check which branch is currently active, (3) See last commit on each branch for comparison, (4) Verify branch exists before operations. CURRENT BRANCH: Look for is_current=true to identify active branch. INDEX STATUS: Shows indexing state per branch (indexed, pending, not_indexed). REMOTE TRACKING: Indicates if branch tracks remote (origin/main, etc.). WORKS WITH: Both global read-only repos ('-global' suffix) and your activated repos (custom aliases). TROUBLESHOOTING: Empty list? Repository might not be initialized or have no branches. RELATED TOOLS: switch_branch (change active branch), git_branch_list (git operation alternative), get_repository_status (includes current branch), git_branch_create (create new branch).