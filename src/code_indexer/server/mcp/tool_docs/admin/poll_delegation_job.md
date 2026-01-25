---
name: poll_delegation_job
category: admin
required_permission: query_repos
tl_dr: Wait for delegation job completion and retrieve results.
inputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: Job ID from execute_delegation_function
    timeout_seconds:
      type: number
      description: 'How long to wait for callback in seconds. Default: 45 (safely below MCP''s 60s timeout). Range: 0.01-300
        (recommended: 5-300 for production). If timeout occurs, returns status=''waiting'' with continue_polling=true - you
        can retry the same job_id to get cached result.'
  required:
  - job_id
  additionalProperties: false
outputSchema:
  type: object
  properties:
    status:
      type: string
      enum:
      - in_progress
      - completed
      - failed
      description: Current job status
    phase:
      type: string
      enum:
      - repo_registration
      - repo_cloning
      - cidx_indexing
      - job_running
      - done
      description: Current phase of job execution
    progress:
      type: object
      description: Phase-specific progress metrics
    message:
      type: string
      description: Human-readable status message
    result:
      type: string
      description: Final result (only when completed)
    error:
      type: string
      description: Error message (only when failed)
    continue_polling:
      type: boolean
      description: Whether to continue polling for updates
    success:
      type: boolean
      description: False if request failed (job not found, not configured)
---

Wait for delegation job completion and retrieve results. Use this tool after execute_delegation_function to get the AI's response. 

HOW IT WORKS: This tool uses a callback-based mechanism for efficiency. Instead of repeatedly polling Claude Server, it waits for Claude Server to notify CIDX when the job completes. This means results are returned immediately when available. 

TIMEOUT BEHAVIOR: If the job doesn't complete within timeout_seconds, returns status='waiting' with continue_polling=true. The job is NOT lost - simply call this tool again with the same job_id. Results are cached, so if the callback arrived while you were timing out, the next call returns immediately with the result. 

POLLING STRATEGY:
1. Call poll_delegation_job with the job_id from execute_delegation_function
2. Check 'continue_polling' field in response:
   - true: Job still in progress, call again after a short delay
   - false: Job completed or failed, stop polling


RESPONSE FIELDS:
- status: 'waiting', 'completed', or 'failed'
- result: The AI's response (only when status='completed')
- error: Error message (only when status='failed')
- message: Human-readable status message
- continue_polling: Whether to continue polling