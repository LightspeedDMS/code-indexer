---
name: trigger_reindex
category: repos
required_permission: repository:write
tl_dr: Trigger manual re-indexing for specified index types.
slim_description: "Trigger re-indexing for specified index types on one of your activated repositories, with optional full rebuild. temporal is rejected for activated repositories."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Alias of one of your activated repositories (not a -global alias)
    index_types:
      type: array
      items:
        type: string
        enum:
        - semantic
        - fts
        - temporal
        - scip
      description: 'Array of index types to rebuild: semantic (embeddings), fts (full-text), scip (call graphs). temporal is listed but rejected for activated repositories.'
    clear:
      type: boolean
      description: 'Rebuild from scratch (true) or incremental update (false). Default: false'
      default: false
  required:
  - repository_alias
  - index_types
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    job_id:
      type: string
      description: Background job ID for tracking
    status:
      type: string
      description: Initial job status
    index_types:
      type: array
      items:
        type: string
      description: Index types being rebuilt
    started_at:
      type: string
      description: Job start time (ISO 8601)
    estimated_duration_minutes:
      type: integer
      description: Estimated completion time in minutes
  required:
  - success
---

TL;DR: Trigger manual re-indexing of one of your activated repositories (a workspace created with activate_repository). The job runs in the background; track it with get_job_details(job_id=...). USE CASES: (1) Rebuild corrupted indexes, (2) Add an index type (e.g., SCIP), (3) Refresh indexes after bulk code changes.

INDEX TYPES: semantic (embedding vectors), fts (full-text search), scip (code intelligence). temporal is rejected: temporal data for an activated repository comes from its golden repository and is never built in the workspace.

CLEAR FLAG: When clear=true, completely rebuilds from scratch (slower but thorough). When false, performs incremental update.

RETURNS: {"success": true, "job_id": "<job id>", "status": "queued", "index_types": [...], "started_at": "<ISO 8601>", "estimated_duration_minutes": N}. estimated_duration_minutes is a fixed per-type figure (semantic, fts: 5 each; scip: 2), not computed from the repository size.

ERRORS (`{"success": false, "error": "..."}`): `repository_alias is required`; `index_types is required`; `Invalid index type(s): <types>. Valid types: ...`; `temporal indexing is not supported for activated repositories: ...`; `Repository directory not found: <path>` (an alias that is not one of your workspaces, including any '-global' alias); `Another reindex job is already running/pending (job <id>). Please wait for it to complete before starting a new reindex.` (the check covers any pending or running reindex job of yours, not only for this repository).

PERMISSIONS: Requires repository:write.

EXAMPLE: {"repository_alias": "my-work", "index_types": ["semantic", "fts"], "clear": false} Returns: {"success": true, "job_id": "<job id>", "status": "queued", "index_types": ["semantic", "fts"], ...}
