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

TL;DR: [SCIP Code Intelligence] Get smart, curated file list for understanding a symbol. Returns prioritized files with relevance scoring - files containing definition, direct dependencies/dependents, and related symbols. Perfect for 'what files should I read to understand X?' Use this before reading code. SYMBOL FORMAT: Pass simple names like 'UserService', 'authenticate', 'DatabaseManager'. SCIP will match fuzzy by default. Full SCIP format like 'scip-python python code-indexer abc123 `module`/ClassName#method().' is handled internally - you only provide the readable part. WHEN TO USE: Getting curated list of files to read for understanding a symbol. Prioritized file list before code review. Understanding symbol context without reading entire codebase. Building mental model of symbol's ecosystem. Finding related code for refactoring. Efficient context gathering for code analysis. WHEN NOT TO USE: Finding all usages (use scip_references). Impact analysis (use scip_impact). Finding dependencies (use scip_dependencies). Finding dependents (use scip_dependents). Tracing call paths (use scip_callchain). Finding definitions (use scip_definition). REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_definition (find definition first), scip_impact (full dependency tree), scip_dependencies (what symbol depends on), scip_dependents (what depends on symbol). EXAMPLE: {"symbol": "DatabaseManager", "limit": 20, "min_score": 0.0} returns {"target_symbol": "com.example.DatabaseManager", "summary": "Read these 3 file(s) - 1 HIGH priority, 2 MEDIUM priority", "total_files": 3, "total_symbols": 8, "avg_relevance": 0.75, "files": [{"path": "src/code_indexer/scip/database/schema.py", "project": "code-indexer", "relevance_score": 1.0, "read_priority": 1, "symbols": [{"name": "DatabaseManager", "kind": "class", "relationship": "definition", "line": 13, "column": 0, "relevance": 1.0}]}]}