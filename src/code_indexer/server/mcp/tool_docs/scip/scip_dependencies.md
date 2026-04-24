---
name: scip_dependencies
category: scip
required_permission: query_repos
tl_dr: Find what a symbol depends on (imports, calls, inherits from).
---

Find what a symbol depends on (imports, calls, inherits from). Shows outgoing dependencies from a symbol.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Understanding what a symbol relies on, mapping import chains, pre-change analysis.
NOT FOR: Finding what depends ON this symbol (scip_dependents), full call chains (scip_callchain).

EXAMPLE: scip_dependencies(symbol='AuthService') -> [{symbol: 'DatabaseClient', kind: 'import'}, ...]