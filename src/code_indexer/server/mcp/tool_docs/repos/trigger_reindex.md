---
name: trigger_reindex
category: repos
required_permission: repository:write
tl_dr: Trigger manual re-indexing for specified index types.
---

TL;DR: Trigger manual re-indexing for specified index types. USE CASES: (1) Rebuild corrupted indexes, (2) Add new index types (e.g., SCIP), (3) Refresh indexes after bulk code changes. INDEX TYPES: semantic (embedding vectors), fts (full-text search), temporal (git history), scip (code intelligence). CLEAR FLAG: When clear=true, completely rebuilds from scratch (slower but thorough). When false, performs incremental update. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "index_types": ["semantic", "fts"], "clear": false} Returns: {"success": true, "job_id": "abc123", "status": "started", "index_types": ["semantic", "fts"]}