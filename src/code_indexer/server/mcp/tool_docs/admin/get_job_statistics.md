---
name: get_job_statistics
category: admin
required_permission: query_repos
tl_dr: Get server-wide counts of background jobs (active/pending/failed).
slim_description: "Get aggregate counts of background jobs without parameters."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    statistics:
      type: object
      description: Job statistics
      properties:
        active:
          type: integer
          description: Number of currently running jobs
        pending:
          type: integer
          description: Number of queued jobs waiting to run
        failed:
          type: integer
          description: Number of failed jobs
        total:
          type: integer
          description: Total jobs (active + pending + failed)
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Get server-wide counts of background jobs: `{"success": true, "statistics": {"active": N, "pending": N, "failed": N, "total": N}}`. The counts cover every job type (repository registration, activation, sync, refresh, indexing and others) and every user's jobs, not only the caller's. `active` counts running jobs, `pending` counts queued jobs, `failed` counts every failed job still retained in the job store (not only recent ones), and `total` is their sum. Returns counts, not individual job details.

To follow one operation, use get_job_details(job_id=...) with the job_id it returned; a zero `active`/`pending` count only shows that no job of any kind is running server-wide. FAILURE HANDLING: When a job of yours fails, get_job_details returns its error message. Common causes for repository jobs: (1) Invalid/inaccessible Git URL, (2) Authentication required for private repo, (3) Network timeout during clone, (4) Disk space issues.
