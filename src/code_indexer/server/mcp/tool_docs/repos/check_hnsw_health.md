---
name: check_hnsw_health
category: repos
required_permission: query_repos
tl_dr: Check HNSW index health and integrity for a repository.
slim_description: "Check HNSW vector index integrity for a repository, validating file existence, readability, loadability, and graph connectivity."
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
        orphan_count:
          type: integer
          description: >-
            Number of zero-inbound-connection (orphan) nodes. Zero-tolerance
            binary signal (Story #1359): orphan_count == 0 is OK, any
            orphan_count > 0 is ERROR (reflected in valid=false) -- there is
            no intermediate WARNING tier and no configurable threshold.
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
    collections:
      type: array
      description: >-
        Present ONLY when more than one real HNSW collection is discovered
        for the repository (multi-provider embedding config and/or temporal
        quarterly shards). Each entry reports one discovered collection so
        none are silently dropped. When present, the top-level "health"
        field mirrors collections[0]["health"] for backward compatibility;
        callers that need every collection's status MUST read this field.
      items:
        type: object
        properties:
          collection_path:
            type: string
            description: >-
              Path to the collection's hnsw_index.bin, relative to the
              repository's clone root.
          health:
            type: object
            description: Same shape as the top-level "health" object.
    error:
      type: string
      description: Error message if failed
  required:
    - success
---

Comprehensive HNSW vector index health check: file existence, readability, loadability, and graph integrity validation.

CACHING: Results cached for 5 minutes. Use force_refresh=true to bypass.

TROUBLESHOOTING: valid=false -> check errors array. file_exists=false -> index not built, run indexing. loadable=false -> corrupted, rebuild required (real index filename is hnsw_index.bin).

MULTIPLE COLLECTIONS: a repo with multiple embedding providers configured and/or temporal quarterly shards may have more than one real HNSW collection on disk. When that's the case, the response additionally includes a "collections" array with one entry per discovered collection -- check it (not just the top-level "health" field) to see every collection's status.

USE INSTEAD OF repository_status for general repo info. This tool is specifically for HNSW vector index integrity.
