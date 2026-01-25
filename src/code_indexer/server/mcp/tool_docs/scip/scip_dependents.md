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

TL;DR: [SCIP Code Intelligence] Find what depends on a symbol (reverse dependencies). Returns symbols and files that require/use the target symbol. Opposite of scip_dependencies. SYMBOL FORMAT: Pass simple names like 'UserService', 'authenticate', 'DatabaseManager'. SCIP will match fuzzy by default - 'User' matches 'UserService', 'UserManager', etc. For exact matching, use exact=true. Full SCIP format like 'scip-python python code-indexer abc123 `module`/ClassName#method().' is handled internally - you only provide the readable part. FUZZY VS EXACT MATCHING: Fuzzy (default, exact=false) uses substring matching - 'User' matches 'UserService', 'UserManager', 'UserRepository'. Fast and flexible, best for exploration. Exact (exact=true) uses precise matching - 'UserService' only matches 'UserService'. Slower but guaranteed accuracy, best when you know the exact symbol name. WHEN TO USE: Understanding impact of changing a symbol (what will break). Finding all code that relies on a symbol. Identifying coupling and understanding how widely a symbol is used. Planning refactoring by understanding dependent code. Understanding blast radius before modifying a symbol. WHEN NOT TO USE: Finding what a symbol depends on (use scip_dependencies instead - opposite direction). Finding all usages (use scip_references for raw usage points). Finding definitions (use scip_definition). Full recursive impact analysis (use scip_impact for complete dependency tree). Tracing call paths (use scip_callchain). Getting curated file list (use scip_context). REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_dependencies (opposite direction - what symbol depends on), scip_impact (recursive dependency analysis), scip_references (raw usage points), scip_context (get curated file list). EXAMPLE: {"symbol": "DatabaseManager", "depth": 1, "exact": false} returns [{"symbol": "com.example.SCIPQueryEngine", "project": "code-indexer", "file_path": "src/code_indexer/scip/query/primitives.py", "line": 15, "column": 0, "kind": "dependent", "relationship": "uses", "context": "self.db = DatabaseManager()"}]