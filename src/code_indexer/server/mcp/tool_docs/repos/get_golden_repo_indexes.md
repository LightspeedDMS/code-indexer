---
name: get_golden_repo_indexes
category: repos
required_permission: query_repos
tl_dr: Get structured status of all index types for a golden repository.
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

Get structured status of all index types for a golden repository. Shows which indexes exist (semantic, fts, temporal, scip) with paths and last updated timestamps. USE CASES: (1) Check if index types are available before querying, (2) Verify index addition completed successfully, (3) Troubleshoot missing search capabilities. RESPONSE: Returns exists flag, filesystem path, and last_updated timestamp for each index type. Empty/null values indicate index does not exist yet.