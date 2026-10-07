---
name: list_repositories
category: repos
required_permission: query_repos
tl_dr: List YOUR activated repositories (user workspaces), distinct from global repos.
slim_description: "List the current user's activated repositories (editable user workspaces), optionally filtered by category name."
inputSchema:
  type: object
  properties:
    category:
      type: string
      description: Filter repositories by category name (exact, case-sensitive match). Use "Unassigned" to show repos without a category. Omit to show all repos.
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repositories:
      type: array
      description: Combined list of activated and global repositories, sorted by category priority
      items:
        type: object
        description: Normalized repository information (activated or global)
        properties:
          user_alias:
            type: string
            description: User-visible repository alias (queryable name). For global repos, ends with '-global' suffix
          golden_repo_alias:
            type: string
            description: Base golden repository name (without -global suffix)
          current_branch:
            type:
            - string
            - 'null'
            description: Active branch for activated repos, null for global repos (read-only snapshots)
          is_global:
            type: boolean
            description: True if globally accessible shared repo, false if user-activated repo
          repo_url:
            type:
            - string
            - 'null'
            description: Repository URL (for global repos)
          last_refresh:
            type:
            - string
            - 'null'
            description: ISO 8601 timestamp of last index refresh
          repo_category:
            type:
            - string
            - 'null'
            description: Category name this repository belongs to, or null if unassigned
          is_composite:
            type: boolean
            description: True if this is a composite repository containing multiple repos
          golden_repo_aliases:
            type: array
            description: List of golden repo aliases included in this composite (composite repos only)
            items:
              type: string
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Lists a combined view: YOUR activated repositories (user-specific workspaces, both single-repo activations and composite repositories you've created) PLUS the global repositories (read-only, '{name}-global' aliases). Use the `is_global` field on each entry to distinguish the two. For a global-repos-only list, use list_global_repos instead.

DETAILS:
- Activated entries carry `user_alias`, `golden_repo_alias`, `current_branch`, `is_composite` and, for composites, `golden_repo_aliases`. An entry also carries `deactivation_job` (`{"job_id", "status"}`) while a deactivation job for it is pending or running, otherwise null.
- Global entries carry `user_alias` (the `-global` alias), `golden_repo_alias`, `repo_url`, `last_refresh`, `current_branch: null` and `is_global: true`.
- Every entry gets `repo_category` (null when unassigned). Entries are sorted by category priority, then by `user_alias`; unassigned entries come last.
- When group-based access control is configured, entries whose golden repository the caller cannot access are removed.

KEY DIFFERENCE FROM list_global_repos:
- list_repositories: Combined view -- YOUR activated repos (editable, user-specific, custom branches) AND global repos
- list_global_repos: Global repos ONLY (read-only, default branches)

USE CASES: See which repositories you've activated for editing or branch-specific work. Find your custom repository aliases to use in file CRUD or git operations. Check if you have an activation before trying to edit files. If no entry has `is_global: false`, you have no activated repositories yet - use activate_repository first.
