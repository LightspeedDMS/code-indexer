---
name: get_index_status
category: repos
required_permission: repository:read
tl_dr: Query current index status for all index types in a repository.
---

TL;DR: Query current index status for all index types in a repository. USE CASES: (1) Check if indexes exist before querying, (2) Verify index freshness, (3) Monitor index health. RETURNS: Status for each index type (semantic, fts, temporal, scip) including: existence, file count, last updated timestamp, size. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "semantic": {"exists": true, "document_count": 1500}, "fts": {"exists": true}, "scip": {"exists": false}}