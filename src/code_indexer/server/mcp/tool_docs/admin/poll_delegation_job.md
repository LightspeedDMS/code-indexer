---
name: poll_delegation_job
category: admin
required_permission: query_repos
tl_dr: Non-blocking check for delegation job result.
---

Non-blocking check for delegation job result. Returns immediately with result or waiting status. Use this tool after execute_delegation_function or execute_open_delegation to get the AI's response.

HOW IT WORKS: This tool uses a callback-based mechanism. Claude Server POSTs results back to CIDX when a job completes. This tool checks whether the result has arrived yet — it returns immediately either way. If the result is not ready, call again after a short delay.

POLLING STRATEGY:
1. Call poll_delegation_job with the job_id from execute_delegation_function or execute_open_delegation
2. Check 'continue_polling' field in response:
   - true: Job still in progress, wait a moment and call again
   - false: Job completed or failed, stop polling
3. Repeat until continue_polling is false

The job stays tracked between calls so multiple retrieval attempts work correctly. Results are cached after the first callback, so repeated polls after completion return the same result.


RESPONSE FIELDS:
- status: 'waiting', 'completed', or 'failed'
- result: The AI's response (only when status='completed' and has_more=false)
- preview: First 2000 chars of result (only when status='completed' and has_more=true)
- has_more: true if result was truncated due to size, false otherwise (only when status='completed')
- cache_handle: handle string for retrieving full result via get_cached_content (only when has_more=true), null when has_more=false
- total_size: total character count of full result (only when has_more=true)
- error: Error message (only when status='failed')
- message: Human-readable status message
- continue_polling: Whether to continue polling

LARGE RESULTS: When the delegation result exceeds 2000 characters, 'result' is replaced by 'preview' (first 2000 chars). Use get_cached_content with the cache_handle to retrieve the full result in pages.
