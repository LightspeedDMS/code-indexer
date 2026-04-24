---
name: scip_definition
category: scip
required_permission: query_repos
tl_dr: Find where a symbol is defined (class, function, method).
---

Find where a symbol is defined (class, function, method). Returns file path, line number, and symbol kind.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Locating definitions, first step before scip_references/scip_dependencies.
NOT FOR: Finding usages (scip_references), dependencies (scip_dependencies), impact analysis (scip_impact).

EXAMPLE: scip_definition(symbol='DatabaseManager') -> file_path, line, kind='class'