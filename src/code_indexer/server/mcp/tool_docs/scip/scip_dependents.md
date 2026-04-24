---
name: scip_dependents
category: scip
required_permission: query_repos
tl_dr: Find what depends on a symbol (what calls it, imports it, inherits from it).
---

Find what depends on a symbol (what calls it, imports it, inherits from it). Shows incoming dependencies to a symbol.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Impact analysis before changes, finding consumers of an API, understanding coupling.
NOT FOR: Finding what this symbol depends on (scip_dependencies), tracing specific paths (scip_callchain).

EXAMPLE: scip_dependents(symbol='DatabaseClient') -> [{symbol: 'AuthService', kind: 'call'}, ...]