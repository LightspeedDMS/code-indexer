---
name: scip_callchain
category: scip
required_permission: query_repos
tl_dr: 'Trace the call chain between two symbols, showing how execution flows from
  caller to callee through intermediate functions.


  Pass simple symbol names (e.g., ''UserService'').'
---

Trace the call chain between two symbols, showing how execution flows from caller to callee through intermediate functions.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

KNOWN LIMITATION: FastAPI route decorators (@app.get, @router.post) are not tracked as call sites. Call chains through FastAPI endpoints may show gaps. Use scip_references to find route handler registrations instead.

USE FOR: Tracing execution paths, understanding how A calls B (directly or transitively), debugging call flows.
NOT FOR: Finding all usages (scip_references), impact analysis of changes (scip_impact).

EXAMPLE: scip_callchain(from_symbol='handle_request', to_symbol='execute_query') -> [{caller, callee, file, line}, ...]