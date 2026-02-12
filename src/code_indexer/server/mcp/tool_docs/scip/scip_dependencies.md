---
name: scip_dependencies
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Find what a symbol depends on (imports, calls, uses).'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to find dependencies for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
    depth:
      type: integer
      default: 1
      description: Dependency traversal depth. Default 1 for direct dependencies only.
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
      description: Total number of dependencies found
    results:
      type: array
      description: List of dependency symbols
      items:
        type: object
        properties:
          symbol:
            type: string
            description: Full SCIP symbol identifier of dependency
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
            description: Symbol kind (dependency)
          relationship:
            type:
            - string
            - 'null'
            description: Relationship type (import, call, use, etc.)
          context:
            type:
            - string
            - 'null'
            description: Code context where dependency occurs
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

Find what a symbol depends on (imports, calls, inherits from). Shows outgoing dependencies from a symbol.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Understanding what a symbol relies on, mapping import chains, pre-change analysis.
NOT FOR: Finding what depends ON this symbol (scip_dependents), full call chains (scip_callchain).

EXAMPLE: scip_dependencies(symbol='AuthService') -> [{symbol: 'DatabaseClient', kind: 'import'}, ...]