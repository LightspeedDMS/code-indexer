---
name: check_hnsw_health
category: repos
required_permission: query_repos
tl_dr: Check HNSW index health and integrity for a repository.
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
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    health:
      type: object
      description: HNSW index health information
      properties:
        valid:
          type: boolean
          description: Overall health status - True if all checks pass
        file_exists:
          type: boolean
          description: Whether index file exists on disk
        readable:
          type: boolean
          description: Whether index file is readable
        loadable:
          type: boolean
          description: Whether index can be loaded by hnswlib
        element_count:
          type: integer
          description: Number of vectors in index
        connections_checked:
          type: integer
          description: Total neighbor connections validated
        min_inbound:
          type: integer
          description: Minimum incoming connections across nodes
        max_inbound:
          type: integer
          description: Maximum incoming connections across nodes
        index_path:
          type: string
          description: Path to index file
        file_size_bytes:
          type: integer
          description: Index file size in bytes
        last_modified:
          type: string
          description: File modification timestamp (ISO 8601)
        errors:
          type: array
          items:
            type: string
          description: List of integrity violations or errors
        check_duration_ms:
          type: number
          description: Time taken for health check in milliseconds
        from_cache:
          type: boolean
          description: Whether result was returned from cache
    error:
      type: string
      description: Error message if failed
  required:
    - success
---

TL;DR: Check HNSW index health and integrity for a repository. Performs comprehensive health check including file existence, readability, HNSW loadability, and integrity validation.

QUICK START: check_hnsw_health(repository_alias='backend-global') returns health status.

USE CASES:
(1) Verify HNSW index integrity before queries
(2) Diagnose index corruption or issues
(3) Monitor index health as part of system checks
(4) Validate index after repository sync or refresh

OUTPUT FIELDS:
- valid: Overall health (True if all checks pass)
- file_exists, readable, loadable: Progressive validation flags
- element_count: Number of vectors indexed
- connections_checked: Total neighbor connections validated
- min_inbound/max_inbound: Connection distribution metrics
- errors: List of integrity violations found
- check_duration_ms: Performance metric
- from_cache: Whether result came from 5-minute cache

CACHING: Results cached for 5 minutes by default. Use force_refresh=true to bypass cache and perform fresh check.

TROUBLESHOOTING:
- valid=false: Check errors array for specific issues
- file_exists=false: Index not built, run indexing first
- readable=false: Permission issues on index file
- loadable=false: Corrupted index, rebuild required
- Integrity errors in errors array: Potential corruption, consider reindexing

PERFORMANCE:
- Cached: <10ms
- Fresh check (46K vectors): ~60ms
- Fresh check (408K vectors): ~638ms
- Large indexes: proportional to vector count

WHEN TO USE:
Before critical operations that depend on index integrity, when diagnosing search issues, after repository sync/refresh operations, as part of system health monitoring.

WHEN NOT TO USE:
For general repository status use get_repository_status or global_repo_status instead. This tool is specifically for HNSW index health checking.

RELATED TOOLS:
get_repository_status (general repo status), global_repo_status (global repo info), get_index_status (all index types), check_health (overall system health)
