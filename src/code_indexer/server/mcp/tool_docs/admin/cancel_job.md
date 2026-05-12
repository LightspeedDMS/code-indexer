---
name: cancel_job
category: admin
required_permission: query_repos
tl_dr: Cancel a running or pending background job. XRay jobs (xray_search, xray_explore) get real process termination; other job types use cooperative cancellation.
slim_description: "Cancel a background job by job_id. Terminates xray processes immediately."
inputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: The unique identifier of the job to cancel (UUID format)
  required:
  - job_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the cancellation succeeded
    message:
      type: string
      description: Human-readable result message
  required:
  - success
  - message
---

TL;DR: Cancel a running or pending background job using its job_id. QUICK START: cancel_job(job_id). CANCELLATION TYPES: Real process termination (SIGTERM then SIGKILL after 2s grace period) for xray_search and xray_explore jobs that have spawned driver processes. Cooperative flag-only cancellation for all other job types (add_golden_repo, refresh_golden_repo, etc.) where the job function checks the cancelled flag periodically. AUTHORIZATION: Users can cancel their own jobs. Admin users can cancel any user's jobs. RESPONSES: Success returns {"success": true, "message": "Job cancelled successfully"}. Failures: job not found or not authorized, cannot cancel job in completed/failed/cancelled status, job_id is required. RELATED TOOLS: get_job_details (check job status after cancellation), get_job_statistics (overview of all jobs), xray_search (submits cancellable xray jobs), xray_explore (submits cancellable xray jobs).
