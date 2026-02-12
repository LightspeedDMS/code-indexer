---
name: scip_references
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Find all places where a symbol is used/referenced (imports, calls, instantiations).'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to find references for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
    limit:
      type: integer
      default: 100
      description: Maximum number of references to return. Default 100.
    exact:
      type: boolean
      default: false
      description: Use exact matching instead of fuzzy substring matching. Default false for flexible exploration.
    project:
      type:
      - string
      - 'null'
      default: null
      description: Optional project filter to limit search to specific project
    repository_alias:
      type:
      - string
      - 'null'
      default: null
      description: Repository alias to filter SCIP search. String for single repo, null/omit for all repos.
  required:
  - symbol
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the operation succeeded
    symbol:
      type: string
      description: Symbol name that was searched for
    total_results:
      type: integer
      description: Total number of references found
    results:
      type: array
      description: List of reference locations
      items:
        type: object
        properties:
          symbol:
            type: string
            description: Full SCIP symbol identifier
          project:
            type: string
            description: Project path
          file_path:
            type: string
            description: File path relative to project root
          line:
            type: integer
            description: Line number (1-indexed)
          column:
            type: integer
            description: Column number (0-indexed)
          kind:
            type: string
            description: Symbol kind (reference)
          relationship:
            type:
            - string
            - 'null'
            description: Relationship type (import, call, instantiation, etc.)
          context:
            type:
            - string
            - 'null'
            description: Code context where reference occurs
        required:
        - symbol
        - project
        - file_path
        - line
        - column
        - kind
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - results
---

Find all locations where a symbol is used (called, imported, referenced). Returns list of file paths, line numbers, and reference kinds.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Finding all usages of a symbol, understanding code coupling, impact of changes.
NOT FOR: Finding where symbol is defined (scip_definition), analyzing call chains (scip_callchain).

EXAMPLE: scip_references(symbol='authenticate') -> [{file_path, line, kind='call'}, ...]