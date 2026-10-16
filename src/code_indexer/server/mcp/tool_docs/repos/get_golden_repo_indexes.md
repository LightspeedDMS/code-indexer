---
name: get_golden_repo_indexes
category: repos
required_permission: query_repos
tl_dr: Get structured status of all index types for a golden repository.
slim_description: "Get index existence, path, and last_updated for semantic, fts, temporal, and scip indexes of a golden repo."
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Golden repository alias (base name, not '-global' suffix)
  required:
  - alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    alias:
      type: string
      description: Golden repository alias
    indexes:
      type: object
      description: Status of each index type
      properties:
        semantic:
          type: object
          description: Semantic search index (embedding-based)
          properties:
            exists:
              type: boolean
            path:
              type:
              - string
              - 'null'
            last_updated:
              type:
              - string
              - 'null'
        fts:
          type: object
          description: Full-text search index (Tantivy)
          properties:
            exists:
              type: boolean
            path:
              type:
              - string
              - 'null'
            last_updated:
              type:
              - string
              - 'null'
        temporal:
          type: object
          description: Temporal index (git history)
          properties:
            exists:
              type: boolean
            path:
              type:
              - string
              - 'null'
            last_updated:
              type:
              - string
              - 'null'
        scip:
          type: object
          description: SCIP index (call graph/code intelligence)
          properties:
            exists:
              type: boolean
            path:
              type:
              - string
              - 'null'
            last_updated:
              type:
              - string
              - 'null'
    error:
      type: string
      description: Error message if operation failed (alias not found)
  required:
  - success
---

Get structured status of all index types for a golden repository. Shows which indexes exist (semantic, fts, temporal, scip) with paths and last updated timestamps. USE CASES: (1) Check if index types are available before querying, (2) Verify index addition completed successfully, (3) Troubleshoot missing search capabilities. RESPONSE: `{"success": true, "alias": "<alias>", "indexes": {"semantic": {...}, "fts": {...}, "temporal": {...}, "scip": {...}}}`, each with `exists`, `path` and `last_updated`; `path` and `last_updated` are null when the index does not exist. For temporal, `path` and `last_updated` describe the repository's `.code-indexer/index` directory (shared with semantic), not the temporal index's own storage location; use `exists` for temporal availability. ALIAS: Pass the golden repository alias without '-global'. ERRORS: `Missing required parameter: alias`; `Golden repository '<alias>' not found`.
