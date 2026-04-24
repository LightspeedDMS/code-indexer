---
name: list_repositories
category: repos
required_permission: query_repos
tl_dr: List YOUR activated repositories (user workspaces), distinct from global repos.
inputSchema:
  type: object
  properties:
    category:
      type: string
      description: Filter repositories by category name. Use "Unassigned" to show repos without a category. Omit to show all repos.
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

Lists YOUR activated repositories (user-specific workspaces). Shows both single-repo activations and composite repositories you've created with user_alias, current_branch, and activation status. Does NOT show global repositories (use list_global_repos for that).

KEY DIFFERENCE FROM list_global_repos:
- list_repositories: YOUR activated repos (editable, user-specific, custom branches)
- list_global_repos: Shared repos (read-only, available to all users, default branches)

USE CASES: See which repositories you've activated for editing or branch-specific work. Find your custom repository aliases to use in file CRUD or git operations. Check if you have an activation before trying to edit files. Empty list means you haven't activated any repositories yet - use activate_repository first.
