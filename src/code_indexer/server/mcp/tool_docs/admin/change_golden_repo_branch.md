---
name: change_golden_repo_branch
category: admin
required_permission: manage_golden_repos
tl_dr: Change the active branch of a golden repository.
---

Change the active branch of a golden repository. Submits the operation as a background job and returns immediately with a job_id.

PARAMETERS: alias (required) - golden repo alias without -global suffix. branch (required) - target branch name.

BEHAVIOR: (1) Validates repository exists and branch name is syntactically valid, (2) Returns immediately with job_id if a new job was submitted. The background job then: acquires write lock, fetches latest from remote origin, validates target branch exists on remote, checks out and pulls the target branch, re-indexes the repository, creates a new CoW snapshot, atomically swaps alias JSON to point to new snapshot, and updates metadata.

ASYNC: This operation returns immediately with a job_id. Use get_job_details to poll for completion. Returns job_id=null (HTTP 200) if already on the target branch — no job is created.

DUPLICATE JOB: If a change_branch job is already running for this repository, returns an error with existing_job_id. Wait for the existing job to complete before retrying.

ERROR CASES: Repository not found (alias does not exist). Invalid branch name (syntactically invalid). Duplicate job already running (use existing_job_id to poll). Git operation failure (network, permissions) — job status will be 'failed'.

RELATED TOOLS: get_job_details (poll job status), refresh_golden_repo (pull latest on same branch), global_repo_status (check current branch), get_job_statistics (monitor background jobs).
