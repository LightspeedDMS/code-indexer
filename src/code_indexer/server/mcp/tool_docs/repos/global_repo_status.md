---
name: global_repo_status
category: repos
required_permission: query_repos
tl_dr: Get detailed status of specific GLOBAL repository (shared, read-only) including refresh timestamps and temporal indexing
  capabilities.
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Global repository alias (e.g., 'repo-name-global')
  required:
  - alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    error:
      type: string
      description: Error message if failed
    alias:
      type: string
      description: Global repository alias
    repo_name:
      type: string
      description: Repository name
    url:
      type: string
      description: Git repository URL
    last_refresh:
      type:
      - string
      - 'null'
      description: ISO 8601 timestamp of last refresh
    enable_temporal:
      type: boolean
      description: Whether temporal indexing (git history search) is enabled for this repository. If true, time-based queries
        are supported.
  required:
  - success
---

Get detailed status of specific GLOBAL repository (shared, read-only) including refresh timestamps and temporal indexing capabilities. Returns alias, repo_name, url, last_refresh (ISO 8601 timestamp), and enable_temporal (boolean indicating git history search support).

ALIAS REQUIREMENT: Use full '-global' suffix alias (e.g., 'backend-global'). If enable_temporal=true, can use time_range/at_commit parameters in search_code. If false, temporal queries return empty results.

COMPARISON: global_repo_status (global shared repos) vs get_repository_status (your activated repos).