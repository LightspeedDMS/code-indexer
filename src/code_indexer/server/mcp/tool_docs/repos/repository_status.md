---
name: repository_status
category: repos
required_permission: query_repos
tl_dr: 'Unified repo status: auto-detects global vs activated from alias suffix.'
slim_description: "Get status of any repository (global or activated) with optional statistics. Auto-detects kind from -global suffix. Returns pinned envelope with kind discriminator."
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: "Repository alias. Append '-global' suffix for global repos (e.g., 'backend-global'); omit suffix for activated user repos (e.g., 'my-repo')."
    detail:
      type: string
      enum:
      - basic
      - stats
      default: basic
      description: "Level of detail. 'basic' returns status only. 'stats' returns status plus statistics (file counts, storage, health score)."
  required:
  - alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    kind:
      type: string
      enum:
      - global
      - activated
      description: "Discriminator: 'global' for shared read-only repos (-global suffix), 'activated' for user-activated repos."
    detail:
      type: string
      enum:
      - basic
      - stats
      description: Echo of the requested detail level
    status:
      type: object
      description: "Repository status. For kind='activated': same fields as former get_repository_status. For kind='global': alias, repo_name, url, last_refresh, enable_temporal, next_refresh (null when not scheduled), enable_scip. next_refresh and enable_scip are global-repo-only fields (not present for kind='activated')."
    statistics:
      type: object
      description: "Repository statistics (only present when detail='stats'). Same fields as former get_repository_statistics."
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Unified replacement for get_repository_status, global_repo_status, and get_repository_statistics (Story #990 hard-cut).

AUTO-DETECTION: The 'kind' discriminator is set automatically:
- alias ending in '-global' -> kind='global' (shared read-only global repository)
- alias without '-global' suffix -> kind='activated' (your user-activated repository)

ENVELOPE SHAPE:
```json
{
  "success": true,
  "kind": "global" | "activated",
  "detail": "basic" | "stats",
  "status": { ...repository status fields... },
  "statistics": { ...only when detail='stats'... }
}
```

FIELD PRESERVATION:
- For kind='activated': status contains the same fields formerly returned by get_repository_status (user_alias, golden_repo_alias, repo_url, activation_status, file_count, index_size, last_updated, enable_temporal, branches_list, etc.)
- For kind='global': status contains alias, repo_name, url, last_refresh, enable_temporal, next_refresh (null when not scheduled), enable_scip — nested under 'status'. next_refresh and enable_scip are global-repo-only (not present for kind='activated').
- When detail='stats': statistics contains the same fields formerly returned by get_repository_statistics (repository_id, files, storage, activity, health)

MIGRATION TABLE:
- get_repository_status(user_alias=X) -> repository_status(alias=X, detail='basic')
- global_repo_status(alias=X) -> repository_status(alias=X, detail='basic')
- get_repository_statistics(repository_alias=X) -> repository_status(alias=X, detail='stats')

NOTE: get_all_repositories_status is NOT affected by this consolidation — use it for a bulk overview of all repos.
