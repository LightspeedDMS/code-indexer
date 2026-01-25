---
name: deactivate_repository
category: repos
required_permission: activate_repos
tl_dr: Remove a user-specific repository activation and delete associated user indexes.
inputSchema:
  type: object
  properties:
    user_alias:
      type: string
      description: User alias of repository to deactivate
  required:
  - user_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type:
      - string
      - 'null'
      description: Background job ID for tracking deactivation
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Remove a user-specific repository activation and delete associated user indexes. Does NOT affect the underlying golden repository or global indexes shared by other users.

USE CASES:
(1) Clean up repository activations you no longer need to free storage
(2) Remove old branch-specific activations when done with feature work
(3) Delete composite repository configurations that are no longer relevant

WHAT IT DOES:
- Removes repository from your personal activated repositories list
- Deletes all user-specific indexes (composite indexes, branch-specific indexes)
- Frees disk space (typically 100MB-2GB per activation depending on repo size)
- Does NOT affect the golden repository or global indexes used by other users
- Does NOT delete the golden repository itself (use remove_golden_repo for that - requires admin)

REQUIREMENTS:
- Permission: 'activate_repos' (power_user or admin role)
- Must provide exact user_alias you used when activating
- Cannot deactivate global repositories (those ending in '-global')

PARAMETERS:
- user_alias: YOUR alias for the activated repo (not the golden repo alias)
  Example: If you activated 'backend-golden' as 'my-backend', use 'my-backend'

RETURNS:
{
  "success": true,
  "deactivated_alias": "my-backend",
  "indexes_deleted": ["composite", "branch-specific"],
  "space_freed_mb": 1234
}

EXAMPLE:
deactivate_repository(user_alias='my-old-project')
-> Removes 'my-old-project' activation, deletes ~500MB of indexes

COMMON ERRORS:
- "Repository not activated" -> Check alias with list_activated_repos()
- "Permission denied" -> You need 'activate_repos' permission (power_user or admin role)
- "Cannot deactivate global repository" -> Global repos can't be deactivated, only removed (admin only)

RELATED TOOLS:
- activate_repository: Create a new activation
- list_activated_repos: See all your current activations
- remove_golden_repo: Admin tool to delete golden repositories (different from deactivation)
- get_repository_status: Check status before deactivating
