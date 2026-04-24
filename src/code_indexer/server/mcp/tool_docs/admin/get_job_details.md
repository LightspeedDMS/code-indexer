---
name: get_job_details
category: admin
required_permission: query_repos
tl_dr: Get detailed status and progress for a specific background job using job_id.
inputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: The unique identifier of the job to query (UUID format)
  required:
  - job_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the operation succeeded
    job:
      type: object
      description: Job details including status, timestamps, progress, and error information
      properties:
        job_id:
          type: string
          description: Unique job identifier (UUID)
        operation_type:
          type: string
          description: Type of operation (e.g., add_golden_repo, remove_golden_repo)
        status:
          type: string
          description: Current job status (pending, running, completed, failed, cancelled)
        created_at:
          type: string
          description: ISO 8601 timestamp when job was created
        started_at:
          type:
          - string
          - 'null'
          description: ISO 8601 timestamp when job started (null if not started)
        completed_at:
          type:
          - string
          - 'null'
          description: ISO 8601 timestamp when job completed (null if not completed)
        progress:
          type: integer
          description: Job progress percentage (0-100)
        result:
          type:
          - object
          - 'null'
          description: Job result data (null if not completed or failed)
        error:
          type:
          - string
          - 'null'
          description: Error message if job failed (null if no error)
        username:
          type: string
          description: Username of the user who submitted the job
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
---

TL;DR: Get detailed status and progress information for specific background job using job_id. Monitor long-running operations like repository indexing, refresh, or removal. QUICK START: get_job_details(job_id) where job_id comes from add_golden_repo, remove_golden_repo, refresh_golden_repo, add_golden_repo_index. OUTPUT FIELDS: job_id (UUID), operation_type (what operation), status (pending/running/completed/failed/cancelled), created_at, started_at, completed_at (ISO 8601 timestamps), progress (0-100%), result (operation output if completed), error (diagnostic message if failed), username (who submitted job). USE CASES: (1) Monitor repository indexing progress after add_golden_repo, (2) Check if refresh_golden_repo completed successfully, (3) Diagnose job failures with error messages, (4) Track long-running operations. JOB LIFECYCLE: pending → running → completed/failed. Poll this endpoint periodically until status is completed or failed. TROUBLESHOOTING: Job not found? job_id may be expired (old jobs are cleaned up). Job stuck in running? Check error field for issues, or contact admin for server logs. RELATED TOOLS: get_job_statistics (overview of all jobs), add_golden_repo (returns job_id), refresh_golden_repo (returns job_id), remove_golden_repo (returns job_id), add_golden_repo_index (returns job_id).