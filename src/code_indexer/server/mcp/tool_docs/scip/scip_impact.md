---
name: scip_impact
category: scip
required_permission: query_repos
tl_dr: '[SCIP] Recursive impact analysis - find symbols and files affected by change.'
inputSchema:
  type: object
  properties:
    symbol:
      type: string
      description: Symbol name to analyze impact for (e.g., 'UserService', 'authenticate', 'DatabaseManager')
    depth:
      type: integer
      default: 3
      description: Recursive traversal depth for impact analysis. Default 3. Max 10. Higher depth = more complete analysis
        but slower query.
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
    depth_analyzed:
      type: integer
      description: Actual depth analyzed
    total_affected:
      type: integer
      description: Total number of affected symbols
    truncated:
      type: boolean
      description: Whether results were truncated due to size limits
    affected_symbols:
      type: array
      description: List of all symbols affected by changing target symbol
      items:
        type: object
        properties:
          symbol:
            type: string
            description: Full SCIP symbol identifier
          file_path:
            type: string
            description: File path relative to project root
          line:
            type: integer
            description: Line number (1-indexed)
          column:
            type: integer
            description: Column number (0-indexed)
          depth:
            type: integer
            description: Depth level in dependency tree
          relationship:
            type: string
            description: Relationship type (uses, calls, imports, etc.)
          chain:
            type: array
            items:
              type: string
            description: Dependency chain from target to this symbol
        required:
        - symbol
        - file_path
        - line
        - column
        - depth
        - relationship
        - chain
    affected_files:
      type: array
      description: File-level summary of impact
      items:
        type: object
        properties:
          path:
            type: string
            description: File path relative to project root
          project:
            type: string
            description: Project path
          affected_symbol_count:
            type: integer
            description: Number of affected symbols in file
          min_depth:
            type: integer
            description: Minimum depth of affected symbols
          max_depth:
            type: integer
            description: Maximum depth of affected symbols
        required:
        - path
        - project
        - affected_symbol_count
        - min_depth
        - max_depth
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - affected_symbols
  - affected_files
---

Analyze the impact of changing a symbol by finding all directly and transitively affected symbols. Returns a dependency tree showing blast radius.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

DEPTH BEHAVIOR: depth=1 shows direct dependents only. depth=2+ shows transitive dependents (what depends on what depends on the symbol). Default depth varies - start with depth=2 for manageable results.

USE FOR: Pre-change impact analysis, estimating blast radius of refactoring, finding all affected code paths.
NOT FOR: Finding definition (scip_definition), tracing specific A->B paths (scip_callchain).

EXAMPLE: scip_impact(symbol='DatabaseClient', depth=2) -> {direct_dependents: [...], transitive: [...]}