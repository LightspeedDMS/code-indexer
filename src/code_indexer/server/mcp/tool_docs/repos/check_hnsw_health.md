---
name: check_hnsw_health
category: repos
required_permission: query_repos
tl_dr: Check HNSW index health and integrity for a repository (async, returns job_id).
slim_description: "Check HNSW vector index integrity for a repository, validating file existence, readability, loadability, and graph connectivity. Submits a background job and returns immediately."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias to check (e.g., 'backend-global', 'frontend-global')
    force_refresh:
      type: boolean
      description: Bypass cache and perform fresh check (default false)
      default: false
  required:
    - repository_alias
outputSchema:
  type: object
  description: >-
    Immediate response returned by this tool call. The health check itself
    runs as a background job -- poll get_job_details or get_job_statistics
    with the returned job_id for the actual result (see "JOB RESULT SHAPE"
    below).
  properties:
    success:
      type: boolean
      description: Whether the job was submitted successfully
    job_id:
      type: string
      description: Background job ID. Poll via get_job_details to retrieve the health result.
    message:
      type: string
      description: Human-readable submission confirmation (e.g. "Use get_job_details to poll.")
    error:
      type: string
      description: Error message if the request failed (missing parameter, repository not found, or job submission failure)
    existing_job_id:
      type: string
      description: ID of the already-running health-check job (only present when a duplicate job conflict is returned)
  required:
    - success
---

Comprehensive HNSW vector index health check: file existence, readability, loadability, and graph integrity validation. Submits the check as a background job and returns immediately with a job_id -- this avoids blocking on repositories with many HNSW collections (e.g. dozens of temporal quarterly shards), which previously risked exceeding the MCP handler timeout.

PARAMETERS: repository_alias (required) - golden repository alias. force_refresh (optional, default false) - bypass the 5-minute health-check cache.

ASYNC: This tool returns immediately with a job_id. Use get_job_details or get_job_statistics to poll for completion; the job's `result` field (once status is "completed") is the JOB RESULT SHAPE described below.

DUPLICATE JOB: check_hnsw_health shares the same "repository_health_check" job type as the golden-repo REST health-check endpoint (POST /{repo_alias}/health/check). If a health check is already running for this repository from that surface, this call returns success=false with existing_job_id set to the running job's ID -- wait for it to complete before retrying, or poll existing_job_id directly. This tool only resolves golden repos; the activated-repos async endpoint (POST /api/activated-repos/{alias}/health/check) uses a different job type (operation_type="activated_repo_health_check"), so there is no cross-surface dedup with it.

JOB RESULT SHAPE: once the polled job reaches status "completed", its `result` field has this shape:
```
{
  "repo_alias": string,
  "overall_healthy": boolean,          // true if ALL collections are healthy
  "health": object,                    // mirrors collections[0] (or {} if no collections) -- backward-compat convenience field
  "collections": [                     // one entry per discovered HNSW collection -- none silently dropped
    {
      "collection_name": string,       // e.g. "voyage-code-3"
      "index_type": string,            // "semantic", "temporal", or "multimodal"
      "valid": boolean,                // overall health status for this collection
      "file_exists": boolean,
      "readable": boolean,
      "loadable": boolean,
      "element_count": integer | null,
      "connections_checked": integer | null,
      "min_inbound": integer | null,
      "max_inbound": integer | null,
      "orphan_count": integer | null,  // zero-tolerance signal (Story #1359): 0 is OK, any value > 0 is ERROR (reflected in valid) -- no WARNING tier
      "hnswlib_capability_available": boolean | null,  // whether the installed hnswlib has the fork's orphan-repair methods; a SEPARATE signal from orphan_count/valid
      "file_size_bytes": integer | null,
      "errors": [string],
      "check_duration_ms": number
    }
  ],
  "total_collections": integer,
  "healthy_count": integer,
  "unhealthy_count": integer,
  "from_cache": boolean
}
```

CACHING: Collection health results are cached for 5 minutes (shared with the REST/Web health surfaces). Use force_refresh=true to bypass.

TROUBLESHOOTING: valid=false on a collection -> check its errors array. file_exists=false -> index not built, run indexing. loadable=false -> corrupted, rebuild required (real index filename is hnsw_index.bin).

MULTIPLE COLLECTIONS: a repo with multiple embedding providers configured and/or temporal quarterly shards may have more than one real HNSW collection on disk. The job result's "collections" array always reports every discovered collection -- check it (not just the top-level "health" field) to see every collection's status.

USE INSTEAD OF repository_status for general repo info. This tool is specifically for HNSW vector index integrity.

RELATED TOOLS: get_job_details (poll job status and retrieve the result), get_job_statistics (monitor background jobs).
