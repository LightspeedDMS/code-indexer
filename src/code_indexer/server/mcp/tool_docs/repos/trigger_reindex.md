---
name: trigger_reindex
category: repos
required_permission: repository:write
tl_dr: Trigger manual re-indexing for specified index types.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    index_types:
      type: array
      items:
        type: string
        enum:
        - semantic
        - fts
        - temporal
        - scip
      description: 'Array of index types to rebuild: semantic (embeddings), fts (full-text), temporal (git history), scip
        (call graphs)'
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

TL;DR: Trigger manual re-indexing for specified index types. USE CASES: (1) Rebuild corrupted indexes, (2) Add new index types (e.g., SCIP), (3) Refresh indexes after bulk code changes. INDEX TYPES: semantic (embedding vectors), fts (full-text search), temporal (git history), scip (code intelligence). CLEAR FLAG: When clear=true, completely rebuilds from scratch (slower but thorough). When false, performs incremental update. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "index_types": ["semantic", "fts"], "clear": false} Returns: {"success": true, "job_id": "abc123", "status": "started", "index_types": ["semantic", "fts"]}