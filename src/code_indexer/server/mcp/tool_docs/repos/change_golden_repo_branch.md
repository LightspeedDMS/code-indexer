---
name: change_golden_repo_branch
category: repos
required_permission: manage_golden_repos
tl_dr: Change the active branch of a golden repository with automatic re-indexing (async, returns job_id).
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
      description: Whether operation was accepted
    job_id:
      type: string
      description: Background job ID for tracking progress (null if already on target branch)
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if failed
    existing_job_id:
      type: string
      description: ID of the already-running job (only present on 409 duplicate conflict)
  required:
  - success
---

Change the active branch of a golden repository. Submits the operation as a background job and returns immediately with a job_id.

PARAMETERS: alias (required) - golden repo alias without -global suffix. branch (required) - target branch name.

BEHAVIOR: (1) Validates repository exists and branch name is syntactically valid, (2) Returns immediately with job_id if a new job was submitted. The background job then: acquires write lock, fetches latest from remote origin, validates target branch exists on remote, checks out and pulls the target branch, re-indexes the repository, creates a new CoW snapshot, atomically swaps alias JSON to point to new snapshot, and updates metadata.

ASYNC: This operation returns immediately with a job_id. Use get_job_details to poll for completion. Returns job_id=null (HTTP 200) if already on the target branch — no job is created.

DUPLICATE JOB: If a change_branch job is already running for this repository, returns an error with existing_job_id. Wait for the existing job to complete before retrying.

ERROR CASES: Repository not found (alias does not exist). Invalid branch name (syntactically invalid). Duplicate job already running (use existing_job_id to poll). Git operation failure (network, permissions) — job status will be 'failed'.

RELATED TOOLS: get_job_details (poll job status), refresh_golden_repo (pull latest on same branch), global_repo_status (check current branch), get_job_statistics (monitor background jobs).
