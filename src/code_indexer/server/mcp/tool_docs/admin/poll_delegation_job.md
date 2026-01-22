---
name: poll_delegation_job
category: admin
required_permission: query_repos
tl_dr: Wait for delegation job completion and retrieve results.
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