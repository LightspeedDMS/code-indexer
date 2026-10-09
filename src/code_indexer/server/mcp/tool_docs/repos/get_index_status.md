---
name: get_index_status
category: repos
required_permission: repository:read
tl_dr: Query current index status for all index types in a repository.
slim_description: "Query current index status for all index types (semantic, fts, temporal, scip) in a repository."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    repository_alias:
      type: string
      description: Repository alias
    semantic:
      type: object
      description: Semantic index status
      properties:
        exists:
          type: boolean
        last_updated:
          type: string
        document_count:
          type: integer
        size_bytes:
          type: integer
    fts:
      type: object
      description: Full-text search index status
      properties:
        exists:
          type: boolean
        last_updated:
          type: string
        document_count:
          type: integer
        size_bytes:
          type: integer
    temporal:
      type: object
      description: Temporal (git history) index status
      properties:
        exists:
          type: boolean
        last_updated:
          type: string
        document_count:
          type: integer
        size_bytes:
          type: integer
    scip:
      type: object
      description: SCIP (call graph) index status
      properties:
        exists:
          type: boolean
        last_updated:
          type: string
        document_count:
          type: integer
        size_bytes:
          type: integer
  required:
  - success
  - repository_alias
  - semantic
  - fts
  - temporal
  - scip
---

TL;DR: Query current index status for all index types in one of your activated repositories. `repository_alias` is your workspace alias (as listed by list_repositories with `is_global: false`); global `-global` aliases are not resolved by this tool (use repository_status for those). USE CASES: (1) Check if indexes exist before querying, (2) Verify index freshness, (3) Monitor index health.

RETURNS: `{"success": true, "repository_alias": "...", "semantic": {...}, "fts": {...}, "temporal": {...}, "scip": {...}}`. Each index object carries a `status` and, when available, details:
- semantic: `status` (`up_to_date`, `not_indexed` or `error`), `last_indexed`, `file_count`, `index_size_mb`; `error` when status is `error`.
- fts: `status` (`up_to_date` or `not_indexed`), `last_updated`, `document_count`, `index_health`.
- temporal: `status` (`up_to_date`, `stale` when older than the staleness threshold, `not_indexed` or `error`), `last_indexed`, `commit_count`, `date_range`.
- scip: `status` (`SUCCESS`, `not_indexed` or `FAILED`), `project_count`, `last_generated`, `projects`; `error` when status is `FAILED`.

ERRORS (`{"success": false, "error": "..."}`): `repository_alias is required`; `Repository directory not found: <path>` (an alias that is not one of your workspaces, including any '-global' alias).

PERMISSIONS: Requires repository:read.

EXAMPLE: {"repository_alias": "my-work"} Returns: {"success": true, "repository_alias": "my-work", "semantic": {"status": "up_to_date", "file_count": 1500, ...}, "fts": {"status": "up_to_date", ...}, "temporal": {"status": "not_indexed"}, "scip": {"status": "not_indexed", "project_count": 0}}
