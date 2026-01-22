---
name: get_golden_repo_indexes
category: repos
required_permission: query_repos
tl_dr: Get structured status of all index types for a golden repository.
---

Get structured status of all index types for a golden repository. Shows which indexes exist (semantic, fts, temporal, scip) with paths and last updated timestamps. USE CASES: (1) Check if index types are available before querying, (2) Verify index addition completed successfully, (3) Troubleshoot missing search capabilities. RESPONSE: Returns exists flag, filesystem path, and last_updated timestamp for each index type. Empty/null values indicate index does not exist yet.