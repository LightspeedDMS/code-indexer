---
name: list_repositories
category: repos
required_permission: query_repos
tl_dr: List repositories YOU have activated (user-specific workspaces), distinct from global shared repositories.
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
    repositories:
      type: array
      description: Combined list of activated and global repositories
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
          index_path:
            type: string
            description: Filesystem path to repository index
          created_at:
            type:
            - string
            - 'null'
            description: ISO 8601 timestamp when repository was added
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: List repositories YOU have activated (user-specific workspaces), distinct from global shared repositories.

USE CASES:
(1) See which repositories you've activated for editing or branch-specific work
(2) Find your custom repository aliases to use in file CRUD or git operations
(3) Check if you have an activation before trying to edit files

WHAT IT DOES:
- Lists YOUR activated repositories (user-specific workspaces)
- Shows both single-repo activations and composite repositories you've created
- Returns user_alias, branch, and activation status for each
- Does NOT show global repositories (use list_global_repos for that)

REQUIREMENTS:
- Permission: 'query_repos' (all roles)
- No parameters needed - returns only your activations

DIFFERENCE FROM list_global_repos:
- list_repositories: YOUR activated repos (editable, user-specific, custom branches)
- list_global_repos: Shared repos (read-only, available to all users, default branches)

RETURNS:
{
  "success": true,
  "repositories": [
    {
      "user_alias": "my-backend",
      "golden_repo_alias": "backend",
      "current_branch": "feature-123",
      "is_global": false
    }
  ]
}

EXAMPLE:
list_repositories()
-> Returns [{"user_alias": "my-backend", "current_branch": "feature-123"}]

COMMON ERRORS:
- Empty list -> You haven't activated any repositories yet, use activate_repository first
- "Permission denied" -> All roles can use this tool

TYPICAL WORKFLOW:
1. List your activations: list_repositories()
2. Edit files: edit_file(repository_alias='my-backend', ...)
3. Commit changes: git_commit('my-backend', 'Fix bug')
4. Clean up: deactivate_repository('my-backend')

RELATED TOOLS:
- list_global_repos: See shared global repositories
- activate_repository: Create a new activation
- deactivate_repository: Remove an activation
- get_repository_status: Get detailed status of an activation
