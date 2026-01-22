---
name: sync_repository
category: repos
required_permission: activate_repos
tl_dr: Synchronize an activated repository with its golden repository source.
---

TL;DR: Synchronize an activated repository with its golden repository source. Pulls latest changes from the golden repo and optionally re-indexes to reflect updates.

USE CASES:
(1) Update your activated repository when the golden repo has new commits
(2) Ensure your local activation matches the latest golden repo state
(3) Refresh indexes after upstream changes

WHAT IT DOES:
- Performs git pull from golden repository to your activated repo
- Optionally triggers re-indexing to update search indexes with new code
- Preserves your local branch state (won't switch branches)
- Does NOT affect golden repository itself (read-only operation on golden)

REQUIREMENTS:
- Permission: 'activate_repos' (power_user or admin role)
- Repository must be activated (not a global repo)
- Golden repository must exist and be accessible

PARAMETERS:
- user_alias: Your user alias for the activated repository
- reindex: Boolean, default true - Whether to re-index after sync
  Set to false if you just want git pull without waiting for indexing

RETURNS:
{
  "success": true,
  "repository_alias": "my-backend",
  "commits_pulled": 5,
  "files_changed": 23,
  "reindex_triggered": true,
  "reindex_job_id": "abc123"  // if reindex=true
}

EXAMPLE:
sync_repository(user_alias='my-backend', reindex=true)
-> Pulls 5 new commits, updates 23 files, starts re-indexing

COMMON ERRORS:
- "Repository not activated" -> Use list_activated_repos() to check
- "Golden repository not found" -> Golden repo may have been removed
- "Merge conflict" -> You have local changes conflicting with upstream

TYPICAL WORKFLOW:
1. Check status: get_repository_status('my-backend')
2. Sync: sync_repository('my-backend', reindex=true)
3. Wait for reindex: monitor job with background job tools
4. Resume work with updated code

RELATED TOOLS:
- activate_repository: Create activation
- get_repository_status: Check sync status before syncing
- refresh_golden_repo: Update the golden repository itself (admin only)
