---
name: cancel_job
category: admin
required_permission: query_repos
tl_dr: Cancel a running or pending background job. XRay jobs (xray_search, xray_explore) have their driver processes terminated; other job types stop cooperatively.
slim_description: "Cancel a pending or running background job by job_id. XRay driver processes are terminated; other jobs are marked cancelled and stop at their next cancellation check."
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

TL;DR: Cancel a running or pending background job using its job_id. QUICK START: cancel_job(job_id="<job id>"). WHAT HAPPENS: A pending job is marked cancelled immediately. For a running job the cancellation is recorded and: (1) xray_search and xray_explore jobs that spawned driver processes have those processes terminated (SIGTERM, then SIGKILL for survivors after a 2s grace period); (2) other jobs stop cooperatively when they next check for cancellation, and job types that run subprocesses through a cancellation check (for example golden-repository refresh and indexing) also terminate those subprocesses; (3) job types with a registered cancel handler, such as dependency-map analysis, have it invoked. A job running on another cluster node is cancelled in the shared job store and stops when that node next checks. AUTHORIZATION: Users can cancel their own jobs. Admin users can cancel any user's jobs. RESPONSES: Success returns {"success": true, "message": "Job cancelled successfully"}. Failures return success false with message "Job not found or not authorized", "Cannot cancel job in <status> status" (completed, failed or cancelled jobs), "job_id is required" or "Failed to cancel job". RELATED TOOLS: get_job_details (check job status after cancellation), get_job_statistics (overview of all jobs), xray_search (submits cancellable xray jobs), xray_explore (submits cancellable xray jobs).
