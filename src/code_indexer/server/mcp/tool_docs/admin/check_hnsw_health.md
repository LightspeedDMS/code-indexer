---
name: check_hnsw_health
category: admin
required_permission: query_repos
tl_dr: 'Comprehensive HNSW vector index health check: file existence, readability,
  loadability, and graph integrity validation.


  CACHING: Results cached for 5 minutes.'
---

Comprehensive HNSW vector index health check: file existence, readability, loadability, and graph integrity validation.

CACHING: Results cached for 5 minutes. Use force_refresh=true to bypass.

TROUBLESHOOTING: valid=false -> check errors array. file_exists=false -> index not built, run indexing. loadable=false -> corrupted, rebuild required.

USE INSTEAD OF get_repository_status or global_repo_status for general repo info. This tool is specifically for HNSW vector index integrity.
