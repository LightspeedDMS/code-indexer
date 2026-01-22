---
name: get_branches
category: repos
required_permission: query_repos
tl_dr: List all git branches for repository with detailed metadata (current branch,
  last commit info, index status, remote tracking).
---

TL;DR: List all git branches for repository with detailed metadata (current branch, last commit info, index status, remote tracking). Supports both global and activated repositories. QUICK START: get_branches('backend-global') lists all branches. OUTPUT FIELDS: Each branch includes name, is_current (boolean), last_commit (sha, message, author, date), index_status (indexing state), remote_tracking (upstream branch info). USE CASES: (1) Discover available branches before switch_branch, (2) Check which branch is currently active, (3) See last commit on each branch for comparison, (4) Verify branch exists before operations. CURRENT BRANCH: Look for is_current=true to identify active branch. INDEX STATUS: Shows indexing state per branch (indexed, pending, not_indexed). REMOTE TRACKING: Indicates if branch tracks remote (origin/main, etc.). WORKS WITH: Both global read-only repos ('-global' suffix) and your activated repos (custom aliases). TROUBLESHOOTING: Empty list? Repository might not be initialized or have no branches. RELATED TOOLS: switch_branch (change active branch), git_branch_list (git operation alternative), get_repository_status (includes current branch), git_branch_create (create new branch).