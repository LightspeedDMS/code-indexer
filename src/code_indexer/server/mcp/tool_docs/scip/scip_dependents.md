---
name: scip_dependents
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Find what depends on a symbol (reverse dependencies).'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to find dependents for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
    depth:
      type: integer
      default: 1
      description: Dependent traversal depth. Default 1 for direct dependents only.
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
      description: Total number of dependents found
    results:
      type: array
      description: List of dependent symbols
      items:
        type: object
        properties:
          symbol:
            type: string
            description: Full SCIP symbol identifier of dependent
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
            description: Symbol kind (dependent)
          relationship:
            type:
            - string
            - 'null'
            description: Relationship type (uses, calls, imports, etc.)
          context:
            type:
            - string
            - 'null'
            description: Code context where dependent uses target
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

Find what depends on a symbol (what calls it, imports it, inherits from it). Shows incoming dependencies to a symbol.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Impact analysis before changes, finding consumers of an API, understanding coupling.
NOT FOR: Finding what this symbol depends on (scip_dependencies), tracing specific paths (scip_callchain).

EXAMPLE: scip_dependents(symbol='DatabaseClient') -> [{symbol: 'AuthService', kind: 'call'}, ...]