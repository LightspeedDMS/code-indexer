---
name: get_repository_statistics
category: repos
required_permission: query_repos
tl_dr: Get comprehensive statistics for repository including file counts (total/indexed,
  by_language), storage usage (repository_size_bytes, index_size_bytes, embedding_count),
  activity timestamps (created_at, last_sync_at, last_accessed_at, sync_count), and
  health score (0.0-1.0 with issues array).
---

Get comprehensive statistics for repository including file counts (total/indexed, by_language), storage usage (repository_size_bytes, index_size_bytes, embedding_count), activity timestamps (created_at, last_sync_at, last_accessed_at, sync_count), and health score (0.0-1.0 with issues array). Works with both global and activated repositories. Health score 1.0 = perfect, <0.8 may indicate issues.