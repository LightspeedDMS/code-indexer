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

TL;DR: [SCIP Code Intelligence] Find what a symbol depends on (imports, calls, uses). Returns symbols and files that the target symbol requires to function. SYMBOL FORMAT: Pass simple names like 'UserService', 'authenticate', 'DatabaseManager'. SCIP will match fuzzy by default - 'User' matches 'UserService', 'UserManager', etc. For exact matching, use exact=true. Full SCIP format like 'scip-python python code-indexer abc123 `module`/ClassName#method().' is handled internally - you only provide the readable part. FUZZY VS EXACT MATCHING: Fuzzy (default, exact=false) uses substring matching - 'User' matches 'UserService', 'UserManager', 'UserRepository'. Fast and flexible, best for exploration. Exact (exact=true) uses precise matching - 'UserService' only matches 'UserService'. Slower but guaranteed accuracy, best when you know the exact symbol name. WHEN TO USE: Understanding what a symbol needs to work (its dependencies). Identifying imports and external dependencies. Finding all symbols a target symbol calls or uses. Understanding coupling and dependency relationships. Planning refactoring by understanding dependencies. WHEN NOT TO USE: Finding what depends on a symbol (use scip_dependents instead - opposite direction). Finding all usages (use scip_references). Finding definitions (use scip_definition). Impact analysis (use scip_impact for recursive dependency tree). Tracing call paths (use scip_callchain). Getting curated file list (use scip_context). REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_dependents (opposite direction - what depends on symbol), scip_impact (recursive dependency analysis), scip_definition (find symbol definition), scip_context (get curated file list). EXAMPLE: {"symbol": "SCIPQueryEngine", "depth": 1, "exact": false} returns [{"symbol": "com.example.DatabaseManager", "project": "code-indexer", "file_path": "src/code_indexer/scip/query/primitives.py", "line": 15, "column": 0, "kind": "dependency", "relationship": "import", "context": "from code_indexer.scip.database.schema import DatabaseManager"}]