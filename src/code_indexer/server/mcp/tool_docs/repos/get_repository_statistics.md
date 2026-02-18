---
name: get_repository_statistics
category: repos
required_permission: query_repos
tl_dr: 'Get repository stats: file counts, storage, language breakdown, health score.'
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    statistics:
      type: object
      description: Repository statistics (RepositoryStatsResponse model)
      properties:
        repository_id:
          type: string
          description: Repository identifier
        files:
          type: object
          description: File statistics
          properties:
            total:
              type: integer
              description: Total number of files
            indexed:
              type: integer
              description: Number of indexed files
            by_language:
              type: object
              description: File counts by programming language
        storage:
          type: object
          description: Storage statistics
          properties:
            repository_size_bytes:
              type: integer
              description: Total repository size in bytes
            index_size_bytes:
              type: integer
              description: Index size in bytes
            embedding_count:
              type: integer
              description: Number of embeddings stored
        activity:
          type: object
          description: Activity statistics
          properties:
            created_at:
              type: string
              description: Repository creation timestamp (ISO 8601)
            last_sync_at:
              type:
              - string
              - 'null'
              description: Last synchronization timestamp (ISO 8601)
            last_accessed_at:
              type:
              - string
              - 'null'
              description: Last access timestamp (ISO 8601)
            sync_count:
              type: integer
              description: Number of successful syncs
        health:
          type: object
          description: Health assessment
          properties:
            score:
              type: number
              description: Health score between 0.0 and 1.0
            issues:
              type: array
              description: List of identified health issues
              items:
                type: string
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Get comprehensive statistics for repository including file counts (total/indexed, by_language), storage usage (repository_size_bytes, index_size_bytes, embedding_count), activity timestamps (created_at, last_sync_at, last_accessed_at, sync_count), and health score (0.0-1.0 with issues array). Works with both global and activated repositories. Health score 1.0 = perfect, <0.8 may indicate issues.