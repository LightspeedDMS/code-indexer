---
name: scip_impact
category: scip
required_permission: query_repos
tl_dr: Analyze the impact of changing a symbol by finding all directly and transitively
  affected symbols.
---

Analyze the impact of changing a symbol by finding all directly and transitively affected symbols. Returns a dependency tree showing blast radius.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

DEPTH BEHAVIOR: depth=1 shows direct dependents only. depth=2+ shows transitive dependents (what depends on what depends on the symbol). Default depth varies - start with depth=2 for manageable results.

USE FOR: Pre-change impact analysis, estimating blast radius of refactoring, finding all affected code paths.
NOT FOR: Finding definition (scip_definition), tracing specific A->B paths (scip_callchain).

EXAMPLE: scip_impact(symbol='DatabaseClient', depth=2) -> {direct_dependents: [...], transitive: [...]}