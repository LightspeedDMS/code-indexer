---
name: get_index_status
category: repos
required_permission: repository:read
tl_dr: Query current index status for all index types in a repository.
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

TL;DR: Query current index status for all index types in a repository. USE CASES: (1) Check if indexes exist before querying, (2) Verify index freshness, (3) Monitor index health. RETURNS: Status for each index type (semantic, fts, temporal, scip) including: existence, file count, last updated timestamp, size. PERMISSIONS: Requires repository:read. EXAMPLE: {"repository_alias": "my-repo"} Returns: {"success": true, "semantic": {"exists": true, "document_count": 1500}, "fts": {"exists": true}, "scip": {"exists": false}}