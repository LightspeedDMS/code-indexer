---
name: scip_context
category: scip
required_permission: query_repos
tl_dr: 'Get rich context around a symbol: its definition, references, dependencies,
  and dependents in one call.'
---

Get rich context around a symbol: its definition, references, dependencies, and dependents in one call. Combines scip_definition + scip_references + scip_dependencies + scip_dependents.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Getting complete picture of a symbol in one call, initial exploration before deeper analysis.
NOT FOR: Specific targeted queries (use individual scip_definition/references/dependencies/dependents instead).

EXAMPLE: scip_context(symbol='AuthService') -> {definition: {...}, references: [...], dependencies: [...], dependents: [...]}