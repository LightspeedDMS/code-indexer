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

Comprehensive HNSW vector index health check: file existence, readability, loadability, and graph integrity validation.

CACHING: Results cached for 5 minutes. Use force_refresh=true to bypass.

TROUBLESHOOTING: valid=false -> check errors array. file_exists=false -> index not built, run indexing. loadable=false -> corrupted, rebuild required.

USE INSTEAD OF get_repository_status or global_repo_status for general repo info. This tool is specifically for HNSW vector index integrity.
