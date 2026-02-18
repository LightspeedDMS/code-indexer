---
name: scip_callchain
category: scip
required_permission: query_repos
tl_dr: '[SCIP] Trace execution paths from entry point to target symbol.'
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

Trace the call chain between two symbols, showing how execution flows from caller to callee through intermediate functions.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

KNOWN LIMITATION: FastAPI route decorators (@app.get, @router.post) are not tracked as call sites. Call chains through FastAPI endpoints may show gaps. Use scip_references to find route handler registrations instead.

USE FOR: Tracing execution paths, understanding how A calls B (directly or transitively), debugging call flows.
NOT FOR: Finding all usages (scip_references), impact analysis of changes (scip_impact).

EXAMPLE: scip_callchain(from_symbol='handle_request', to_symbol='execute_query') -> [{caller, callee, file, line}, ...]