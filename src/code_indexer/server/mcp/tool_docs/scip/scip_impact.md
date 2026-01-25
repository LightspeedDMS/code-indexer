---
name: scip_impact
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Recursive impact analysis - find ALL symbols and files affected by changing a symbol.'
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

TL;DR: [SCIP Code Intelligence] Recursive impact analysis - find ALL symbols and files affected by changing a symbol. Returns complete dependency tree with depth tracking and file-level summaries. Use this for comprehensive change impact assessment. SYMBOL FORMAT: Pass simple names like 'UserService', 'authenticate', 'DatabaseManager'. SCIP will match fuzzy by default. Full SCIP format like 'scip-python python code-indexer abc123 `module`/ClassName#method().' is handled internally - you only provide the readable part. DEPTH BEHAVIOR: Results grow linearly with depth (BFS traversal with cycle detection prevents exponential growth). depth=1 shows direct dependents, depth=2 adds dependents-of-dependents, depth=3 adds third-level dependents. Use depth=3 (default) for comprehensive analysis, depth=5+ for mission-critical changes requiring complete blast radius understanding. Higher depth increases query time but ensures complete impact visibility. WHEN TO USE: Understanding full blast radius of changing a symbol. Planning refactoring with complete dependency tree visibility. Assessing risk before modifying critical code. Generating file lists for comprehensive testing. Understanding cascading dependencies across multiple levels. Finding all code that transitively depends on a symbol. WHEN NOT TO USE: Finding direct dependencies only (use scip_dependencies for faster single-level query). Finding direct dependents only (use scip_dependents for faster single-level query). Simple usage point lookup (use scip_references). Finding definitions (use scip_definition). Tracing specific call paths (use scip_callchain). Getting prioritized file list for reading (use scip_context). REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_dependents (single-level dependents), scip_dependencies (single-level dependencies), scip_callchain (trace call paths), scip_context (get curated file list with relevance scoring). EXAMPLE: {"symbol": "DatabaseManager", "depth": 3} returns {"target_symbol": "com.example.DatabaseManager", "depth_analyzed": 3, "total_affected": 47, "affected_symbols": [{"symbol": "SCIPQueryEngine", "file_path": "src/code_indexer/scip/query/primitives.py", "line": 15, "column": 0, "depth": 1, "relationship": "uses", "chain": ["DatabaseManager", "SCIPQueryEngine"]}], "affected_files": [{"path": "src/code_indexer/scip/query/primitives.py", "project": "code-indexer", "affected_symbol_count": 3, "min_depth": 1, "max_depth": 2}]}