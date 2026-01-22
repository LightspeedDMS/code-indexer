---
name: get_repository_statistics
category: repos
required_permission: query_repos
tl_dr: Get comprehensive statistics for repository including file counts, storage
  usage, language breakdown, indexing progress, and health score.
---

TL;DR: Get comprehensive statistics for repository including file counts, storage usage, language breakdown, indexing progress, and health score. QUICK START: get_repository_statistics('backend-global') returns full stats. OUTPUT CATEGORIES: (1) files - total/indexed counts, breakdown by_language, (2) storage - repository_size_bytes, index_size_bytes, embedding_count, (3) activity - created_at, last_sync_at, last_accessed_at, sync_count, (4) health - score (0.0-1.0), issues array. USE CASES: (1) Monitor indexing progress (indexed vs total files), (2) Track storage usage and growth, (3) Identify language distribution in codebase, (4) Assess repository health. HEALTH SCORE: 1.0 = perfect health, <0.8 may indicate issues (check issues array for details). WORKS WITH: Both global and activated repositories. TROUBLESHOOTING: Low health score? Check issues array for specific problems (missing indexes, sync failures, etc.). RELATED TOOLS: get_all_repositories_status (summary across all repos), get_repository_status (activation status), get_job_statistics (background job health).