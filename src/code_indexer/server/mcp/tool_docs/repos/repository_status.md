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
      description: "Repository alias. A '-global' alias (e.g., 'example-repo-global') returns global repository status; any other alias is looked up as a golden repository alias (see the tool description)."
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

Get the status of one repository, with optional statistics.

AUTO-DETECTION: The 'kind' discriminator is set from the alias:
- alias ending in '-global' -> kind='global' (shared read-only global repository)
- any other alias -> kind='activated'

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

STATUS FIELDS:
- kind='global': status contains alias, repo_name, url, last_refresh, enable_temporal, next_refresh (null when not scheduled) and enable_scip. An unknown alias returns `Global repo '<alias>' not found`.
- kind='activated': the alias is looked up as a golden repository alias, and status describes that golden repository: alias, repo_url, default_branch, clone_path, created_at, activation_status ('activated' when you have an activation of it, else 'available'), branches_list, file_count, index_size, last_updated, enable_temporal and temporal_status. An alias that exists only as your workspace alias (for example 'my-work') is not a golden repository alias and returns `Repository '<alias>' not found`; use list_repositories for your workspaces.

STATISTICS (detail='stats'):
- kind='global': statistics contains repository_alias, is_global, path and index_path ({} when the alias cannot be resolved).
- kind='activated': statistics contains the repository statistics report (repository_id, files, storage, activity, health).

ERRORS: `Missing required parameter: alias`; `Invalid detail value '<value>': must be 'basic' or 'stats'`; the not-found messages above.

For a bulk overview of all repos, use get_all_repositories_status.
