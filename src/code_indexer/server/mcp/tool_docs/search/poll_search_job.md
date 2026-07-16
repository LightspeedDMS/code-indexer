---
name: poll_search_job
category: search
required_permission: query_repos
tl_dr: Non-blocking check for an async-hybrid temporal search_code job. Returns immediately with partial or final results.
slim_description: "Non-blocking check for a temporal search_code job result by job_id."
inputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: Job ID returned by search_code when a temporal query exceeded the inline sync-wait window
  required:
  - job_id
  additionalProperties: false
outputSchema:
  type: object
  properties:
    status:
      type: string
      enum:
      - waiting
      - completed
      - failed
      - not_found
      description: Current job status
    continue_polling:
      type: boolean
      description: Whether to continue polling for updates
    results:
      type: array
      description: Final results (only when status=completed)
    partial_results:
      type: array
      description: Cumulative partial results so far (only when status=waiting)
    shards_completed:
      type: integer
      description: Number of temporal shards attempted so far
    shards_total:
      type:
      - integer
      - "null"
      description: Total shards scheduled, or null before shard discovery completes
    unranked:
      type: boolean
      description: True when the result order is not a rerank-trustworthy order (always true for partial reads; reflects actual rerank outcome for completed reads)
    error:
      type: string
      description: Error message (only when status=failed or status=not_found)
    success:
      type: boolean
      description: False when the job is not found or not owned by the caller
---

Non-blocking check for an async-hybrid temporal search_code job's result. Returns immediately with the current status either way -- never blocks waiting for the job to progress.

WHEN TO USE: search_code returns a job_id when a temporal query (time_range/at_commit/diff_type/author/chunk_type) exceeds the configured inline sync-wait window (temporal_inline_wait_seconds). Call poll_search_job with that job_id to retrieve partial progress or the final result.

POLLING STRATEGY:
1. Call poll_search_job with the job_id from the search_code handoff response.
2. Check 'continue_polling' in the response:
   - true: job still running, wait a moment and call again. 'partial_results' contains the cumulative results discovered so far (temporal display order, reverse-chronological).
   - false: job reached a terminal state (completed, failed, or not_found) -- stop polling.
3. Repeat until continue_polling is false.

OWNERSHIP: only the user who submitted the original search_code query can poll its job_id. A job_id belonging to another user, or an unknown job_id, returns status=not_found (these two cases are intentionally indistinguishable for privacy).

RESULT EXPIRY: a completed job's result is cached for a limited time (server-configured TTL). Polling a job that completed long ago returns status=not_found with an "expired -- resubmit" error; re-issue the original search_code query as a new request.

UNRANKED FLAG: partial results are ALWAYS unranked=true (any requested reranking only applies to the final, completed result). A completed result's unranked flag reflects whether reranking actually succeeded -- even a rerank-requested query can come back unranked=true if no reranker was configured or the reranker failed.
