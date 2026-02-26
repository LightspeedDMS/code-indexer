---
name: change_golden_repo_branch
category: repos
required_permission: manage_golden_repos
tl_dr: Change the active branch of a golden repository with automatic re-indexing.
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Golden repository alias (without -global suffix)
    branch:
      type: string
      description: Target branch name to switch to
  required:
  - alias
  - branch
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Change the active branch of a golden repository. Performs git fetch, branch validation, checkout, re-indexing, and creates a new CoW snapshot with atomic alias swap.

PARAMETERS: alias (required) - golden repo alias without -global suffix. branch (required) - target branch name.

BEHAVIOR: (1) Validates repository exists and branch differs from current, (2) Acquires write lock (blocks if refresh/indexing in progress), (3) Fetches latest from remote origin, (4) Validates target branch exists on remote, (5) Checks out and pulls the target branch, (6) Re-indexes the repository, (7) Creates a new CoW snapshot, (8) Atomically swaps alias JSON to point to new snapshot, (9) Updates metadata.

SYNCHRONOUS: This operation runs synchronously (no job_id returned). It may take several minutes for large repositories. The write lock prevents concurrent operations.

ERROR CASES: Repository not found (alias does not exist). Branch does not exist on remote. Repository currently being indexed or refreshed (try again later). Git operation failure (network, permissions).

RELATED TOOLS: refresh_golden_repo (pull latest on same branch), global_repo_status (check current branch), get_job_statistics (monitor background jobs).
