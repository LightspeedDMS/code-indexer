---
name: get_job_statistics
category: admin
required_permission: query_repos
tl_dr: Get counts of background repository indexing jobs (active/pending/failed).
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

Get counts of background repository indexing jobs (active/pending/failed). Use this to monitor if repository registration, activation, or sync operations are still in progress. Returns job counts, not individual job details. Example: after calling add_golden_repo, check this periodically - when active=0 and pending=0, indexing is complete. FAILURE HANDLING: If failed>0, common causes: (1) Invalid/inaccessible Git URL, (2) Authentication required for private repo, (3) Network timeout during clone, (4) Disk space issues. For details, admin can check server logs or REST API /api/admin/jobs endpoint.