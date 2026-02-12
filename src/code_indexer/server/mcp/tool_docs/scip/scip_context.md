---
name: scip_context
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Get smart, curated file list for understanding a symbol.'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to get context for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
    limit:
      type: integer
      default: 20
      description: Maximum number of files to return. Default 20. Max 100.
    min_score:
      type: number
      default: 0.0
      description: Minimum relevance score threshold (0.0-1.0). Default 0.0 for all relevant files.
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
    target_symbol:
      type: string
      description: Full SCIP symbol identifier analyzed
    summary:
      type: string
      description: Human-readable summary of results
    total_files:
      type: integer
      description: Total number of files returned
    total_symbols:
      type: integer
      description: Total number of symbols across all files
    avg_relevance:
      type: number
      description: Average relevance score across all files
    files:
      type: array
      description: Prioritized list of files to read, sorted by relevance
      items:
        type: object
        properties:
          path:
            type: string
            description: File path relative to project root
          project:
            type: string
            description: Project path
          relevance_score:
            type: number
            description: Relevance score (0.0-1.0) - higher is more relevant
          read_priority:
            type: integer
            description: Read priority (1=HIGH, 2=MEDIUM, 3=LOW) - lower number means read first
          symbols:
            type: array
            description: Symbols in file related to target symbol
            items:
              type: object
              properties:
                name:
                  type: string
                  description: Symbol name
                kind:
                  type: string
                  description: Symbol kind (class, function, method, etc.)
                relationship:
                  type: string
                  description: Relationship to target (definition, dependency, dependent, reference)
                line:
                  type: integer
                  description: Line number (1-indexed)
                column:
                  type: integer
                  description: Column number (0-indexed)
                relevance:
                  type: number
                  description: Symbol relevance score (0.0-1.0)
              required:
              - name
              - kind
              - relationship
              - line
              - column
              - relevance
        required:
        - path
        - project
        - relevance_score
        - read_priority
        - symbols
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - files
---

Get rich context around a symbol: its definition, references, dependencies, and dependents in one call. Combines scip_definition + scip_references + scip_dependencies + scip_dependents.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Getting complete picture of a symbol in one call, initial exploration before deeper analysis.
NOT FOR: Specific targeted queries (use individual scip_definition/references/dependencies/dependents instead).

EXAMPLE: scip_context(symbol='AuthService') -> {definition: {...}, references: [...], dependencies: [...], dependents: [...]}