---
name: scip_callchain
category: scip
required_permission: query_repos
tl_dr: '[SCIP Code Intelligence] Trace all execution paths from entry point (from_symbol) to target function (to_symbol).'
inputSchema:
  type: object
  properties:
    from_symbol:
      type: string
      description: Starting symbol (e.g., 'handle_request', 'Controller.process')
    to_symbol:
      type: string
      description: Target symbol to reach (e.g., 'DatabaseManager', 'authenticate')
    max_depth:
      type: integer
      default: 10
      description: Maximum chain length to search. Default 10. Max 20. Higher values find longer chains but slower query.
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
  - from_symbol
  - to_symbol
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the operation succeeded
    from_symbol:
      type: string
      description: Starting symbol searched
    to_symbol:
      type: string
      description: Target symbol searched
    total_chains_found:
      type: integer
      description: Total number of call chains found
    truncated:
      type: boolean
      description: Whether results were truncated due to size limits
    max_depth_reached:
      type: boolean
      description: Whether search hit max_depth limit
    chains:
      type: array
      description: List of call chains from source to target
      items:
        type: object
        properties:
          length:
            type: integer
            description: Number of steps in chain
          path:
            type: array
            description: Sequence of call steps from source to target
            items:
              type: object
              properties:
                symbol:
                  type: string
                  description: Symbol at this step
                file_path:
                  type: string
                  description: File path relative to project root
                line:
                  type: integer
                  description: Line number (1-indexed)
                column:
                  type: integer
                  description: Column number (0-indexed)
                call_type:
                  type: string
                  description: Type of call (call, import, instantiation, etc.)
              required:
              - symbol
              - file_path
              - line
              - column
              - call_type
        required:
        - length
        - path
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - chains
---

TL;DR: [SCIP Code Intelligence] Trace all execution paths from entry point (from_symbol) to target function (to_symbol). 

SUPPORTED SYMBOL FORMATS:
- Simple names: "chat", "invoke", "CustomChain", "BaseClient"
- Class#method: "CustomChain#chat", "BaseClient#invoke"
- Full SCIP identifiers: "scip-python python . hash `module`/Class#method()."


USAGE EXAMPLES:
- Method to method: from_symbol="chat", to_symbol="invoke"
- Class to class: from_symbol="CustomChain", to_symbol="BaseClient"
- Within class: from_symbol="CustomChain#chat", to_symbol="CustomChain#_generate_sql"


KNOWN LIMITATIONS:
- May not capture FastAPI endpoint decorators (@app.post, @app.get)
- Factory functions may not show call chains to instantiated methods
- Cross-repository search: omit repository_alias to search all repositories


RESPONSE INCLUDES:
- path: List of symbol names in execution order
- length: Number of hops in the chain
- has_cycle: Boolean indicating if path contains cycles
- diagnostic: Helpful message when no chains found
- scip_files_searched: Number of SCIP indexes searched
- repository_filter: Which repository was searched


TIPS FOR BEST RESULTS:
- Start with simple class or method names
- Use repository_alias to limit search scope
- Increase max_depth if chains seem incomplete (max: 10)
- Check diagnostic message if 0 chains found


REQUIRES: SCIP indexes must be generated via 'cidx scip generate' before querying. Check .code-indexer/scip/ directory for .scip files. RELATED TOOLS: scip_impact (full dependency tree), scip_dependencies (what symbol depends on), scip_dependents (what depends on symbol), scip_context (get curated file list). EXAMPLE: {"from_symbol": "handle_request", "to_symbol": "DatabaseManager", "max_depth": 10} returns {"from_symbol": "handle_request", "to_symbol": "DatabaseManager", "total_chains_found": 2, "chains": [{"length": 3, "path": [{"symbol": "handle_request", "file_path": "src/api/handler.py", "line": 10, "column": 0, "call_type": "call"}, {"symbol": "UserService.authenticate", "file_path": "src/services/user.py", "line": 25, "column": 4, "call_type": "call"}, {"symbol": "DatabaseManager.query", "file_path": "src/database/manager.py", "line": 50, "column": 8, "call_type": "call"}]}]}