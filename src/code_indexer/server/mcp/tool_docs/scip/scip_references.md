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

TL;DR: [SCIP Code Intelligence] Find all places where a symbol is used/referenced (imports, calls, instantiations). Returns file locations, line numbers, and usage context. SYMBOL FORMAT: Pass simple names like 'UserService', 'authenticate', 'DatabaseManager'. SCIP will match fuzzy by default - 'User' matches 'UserService', 'UserManager', etc. For exact matching, use exact=true. Full SCIP format like 'scip-python python code-indexer abc123 `module`/ClassName#method().' is handled internally - you only provide the readable part. FUZZY VS EXACT MATCHING: Fuzzy (default, exact=false) uses substring matching - 'User' matches 'UserService', 'UserManager', 'UserRepository'. Fast and flexible, best for exploration when you want to find all related usages. Exact (exact=true) uses precise matching - 'UserService' only matches 'UserService'. Slower but guaranteed accuracy, best when you know the exact symbol name and want only its references. WHEN TO USE: Finding all code that uses/imports/calls a symbol. Understanding how widespread a symbol's usage is. Identifying all callsites before refactoring. Finding examples of how a symbol is used in practice. WHEN NOT TO USE: Finding where a symbol is defined (use scip_definition instead). Understanding what a symbol depends on (use scip_dependencies). Understanding what depends on a symbol (use scip_dependents - references show usage points, dependents show dependent symbols). Impact analysis (use scip_impact). Tracing call paths (use scip_callchain). Getting curated file list (use scip_context). REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_definition (find definition), scip_dependents (what symbols depend on target), scip_impact (recursive dependency analysis), scip_context (get curated file list). EXAMPLE: {"symbol": "DatabaseManager", "limit": 100, "exact": false} returns [{"symbol": "com.example.DatabaseManager", "project": "code-indexer", "file_path": "src/code_indexer/scip/query/primitives.py", "line": 42, "column": 8, "kind": "reference", "relationship": "import", "context": "from code_indexer.scip.database.schema import DatabaseManager"}]