---
name: scip_definition
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Find where a symbol is defined (class, function, method).'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to find definition for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
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
      description: Total number of definitions found
    results:
      type: array
      description: List of definition locations
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
            description: Symbol kind (class, function, method, variable, etc.)
          relationship:
            type:
            - string
            - 'null'
            description: Relationship type (always null for definitions)
          context:
            type:
            - string
            - 'null'
            description: Additional context (always null for definitions)
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

Find where a symbol is defined (class, function, method). Returns file path, line number, and symbol kind.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Locating definitions, first step before scip_references/scip_dependencies.
NOT FOR: Finding usages (scip_references), dependencies (scip_dependencies), impact analysis (scip_impact).

EXAMPLE: scip_definition(symbol='DatabaseManager') -> file_path, line, kind='class'