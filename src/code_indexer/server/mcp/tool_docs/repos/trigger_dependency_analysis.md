---
name: trigger_dependency_analysis
category: repos
required_permission: manage_repos
tl_dr: Manually trigger dependency map analysis (full or delta mode).
inputSchema:
  type: object
  properties:
    mode:
      type: string
      description: 'Analysis mode: full (complete regeneration) or delta (incremental update). Default: delta'
      enum:
      - full
      - delta
      default: delta
  additionalProperties: false
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
      description: Background job ID for tracking analysis progress
    mode:
      type: string
      description: Analysis mode used (full or delta)
    status:
      type: string
      description: Initial job status (queued, running, or error)
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Manually trigger dependency map analysis on demand.

WHAT IT DOES:
Starts a background analysis job to generate or refresh the dependency map that powers the cidx-meta repository. The dependency map identifies domain boundaries and cross-repository dependencies by analyzing all golden repositories.

MODES:
- full: Complete regeneration of the entire dependency map (slow, thorough)
- delta: Incremental update for repositories with changes since last analysis (fast, targeted)

WHEN TO USE:
- After adding new golden repositories
- After significant code changes across multiple repositories
- When dependency map appears stale or incomplete
- To refresh without waiting for scheduled analysis

ASYNC BEHAVIOR:
Returns immediately with a job_id. Analysis runs in background. Use get_repository_status or check cidx-meta indexing status to monitor progress.

CONCURRENCY:
Only one dependency map analysis can run at a time. Concurrent requests will be rejected with "already in progress" error.

REQUIREMENTS:
- dependency_map_enabled must be True in server configuration
- Requires admin permission (manage_repos)

EXAMPLE:
{"mode": "delta"} Returns: {"success": true, "job_id": "abc123", "mode": "delta", "status": "queued", "message": "Dependency map delta analysis started"}
