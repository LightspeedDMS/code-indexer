---
name: get_repository_status
category: repos
required_permission: query_repos
tl_dr: Get comprehensive status of YOUR activated repository including indexing state, file counts, git branch info, and temporal
  capabilities.
inputSchema:
  type: object
  properties:
    user_alias:
      type: string
      description: User alias of repository
  required:
  - user_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    status:
      type: object
      description: Detailed repository status information
      properties:
        alias:
          type: string
          description: Repository alias
        repo_url:
          type: string
          description: Repository URL
        default_branch:
          type: string
          description: Default branch name
        clone_path:
          type: string
          description: Filesystem path to cloned repository
        created_at:
          type: string
          description: Repository creation timestamp
        activation_status:
          type: string
          description: Activation status (activated/available)
        branches_list:
          type: array
          description: List of available branches
          items:
            type: string
        file_count:
          type: integer
          description: Number of files in repository
        index_size:
          type: integer
          description: Size of index in bytes
        last_updated:
          type: string
          description: Last update timestamp
        enable_temporal:
          type: boolean
          description: Whether temporal indexing is enabled
        temporal_status:
          type:
          - object
          - 'null'
          description: Temporal indexing status (null if disabled)
          properties:
            enabled:
              type: boolean
              description: Whether temporal indexing is enabled
            diff_context:
              type: integer
              description: Number of context lines for diffs
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Get comprehensive status of YOUR activated repository including indexing state, file counts, git branch info, and temporal capabilities. Returns activation_status, file_count, index_size, branches_list, enable_temporal, and last_updated fields.

REQUIRES: User-specific repository activation (use your custom alias, NOT '-global' suffix). For global read-only repositories, use global_repo_status instead. Check enable_temporal field to confirm if time_range queries are supported in search_code.