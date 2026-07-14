---
name: list_global_repos
category: repos
required_permission: query_repos
tl_dr: List all globally accessible repositories.
slim_description: "List all globally accessible repositories registered via add_golden_repo, queryable as '{name}-global' aliases."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repos:
      type: array
      description: List of global repositories (raw registry rows)
      items:
        type: object
        properties:
          alias_name:
            type: string
            description: Global repository alias (unique primary key)
          repo_name:
            type: string
            description: Base repository name
          repo_url:
            type:
            - string
            - 'null'
            description: Repository URL
          index_path:
            type: string
            description: Filesystem path to index
          created_at:
            type:
            - string
            - 'null'
            description: ISO 8601 creation timestamp
          last_refresh:
            type:
            - string
            - 'null'
            description: ISO 8601 last refresh timestamp
          enable_temporal:
            type: boolean
            description: Whether temporal indexing is enabled
          temporal_options:
            type: object
            description: Temporal indexing configuration options
          enable_scip:
            type: boolean
            description: Whether SCIP indexing is enabled
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

List all globally accessible repositories. Returns repos registered via add_golden_repo, immediately queryable as '{name}-global' alias.

TERMINOLOGY: Golden repositories are admin-registered source repos. Global repositories are the publicly queryable versions accessible via '{name}-global' alias.

ABOUT cidx-meta-global: This repository appears in the list but is NOT a code repository. It is a synthetic discovery repository containing AI-generated markdown descriptions of all other registered repositories. Search it first to find which repository covers your topic. See cidx_quick_reference for the full discovery workflow.

DISCOVERY PATTERN: Before listing all repos, search 'cidx-meta-global' to discover which repositories are relevant to your topic: search_code('authentication', repository_alias='cidx-meta-global') returns repos that handle authentication, then query those specific repos for detailed code.

Use list_global_repos() only when explicitly asked for the full repo list or to verify a repo exists. For detailed status of one repo (temporal support, refresh times), use repository_status instead.