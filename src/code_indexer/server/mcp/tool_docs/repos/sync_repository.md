---
name: sync_repository
category: repos
required_permission: activate_repos
tl_dr: Synchronize an activated repository with its golden repository source.
inputSchema:
  type: object
  properties:
    user_alias:
      type: string
      description: User alias of repository
  required:
  - user_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type:
      - string
      - 'null'
      description: Background job ID for tracking sync progress
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Synchronize an activated repository with its golden repository source.

WHAT IT DOES:
Performs git pull from golden repository to your activated repo and re-indexes changed files to update search indexes with new code. Preserves your local branch state (won't switch branches).

ASYNC BEHAVIOR:
Returns immediately with a job_id. Sync and re-indexing happen in background. Check get_repository_status to monitor progress until sync completes.

WHEN TO USE:
After upstream repository changes to pull latest commits and refresh your local activation's indexes with new code.
