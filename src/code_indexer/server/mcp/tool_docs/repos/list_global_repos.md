---
name: list_global_repos
category: repos
required_permission: query_repos
tl_dr: List all globally accessible repositories.
slim_description: "List all globally accessible repositories registered via add_golden_repo, queryable as '{name}-global' aliases."
inputSchema:
  type: object
  properties:
    limit:
      type: integer
      description: "Max repos to return (default 50, max 200). The list is paged to avoid huge responses."
    offset:
      type: integer
      description: "Number of repos to skip for pagination (default 0)."
    name_filter:
      type: string
      description: "Case-insensitive substring; return only repos whose name/alias contains it."
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repos:
      type: array
      description: List of global repositories (normalized schema)
      items:
        type: object
        properties:
          user_alias:
            type: string
            description: Global repository alias (ends with '-global')
          golden_repo_alias:
            type: string
            description: Base repository name
          is_global:
            type: boolean
            description: Always true for global repos
          repo_url:
            type:
            - string
            - 'null'
            description: Repository URL
          last_refresh:
            type:
            - string
            - 'null'
            description: ISO 8601 last refresh timestamp
          index_path:
            type: string
            description: Filesystem path to index
          created_at:
            type:
            - string
            - 'null'
            description: ISO 8601 creation timestamp
    total:
      type: integer
      description: Total repos matching (before limit/offset) for pagination
    offset:
      type: integer
      description: Offset applied to this page
    limit:
      type: integer
      description: Limit applied to this page
    returned:
      type: integer
      description: Number of repos in this page
    has_more:
      type: boolean
      description: True if more repos remain past this page (offset+returned < total)
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

PAGINATION: The response is paged (default 50, max 200 per call). Use total/has_more to page with offset, or pass name_filter to jump straight to a repo by substring instead of paging the whole list. Prefer searching 'cidx-meta-global' (above) over listing everything.