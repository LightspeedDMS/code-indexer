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

Remove a user-specific repository activation and delete associated user indexes.

WHAT IT DOES:
Removes repository from your personal activated repositories list and deletes all user-specific indexes (composite indexes, branch-specific indexes). Frees disk space (typically 100MB-2GB per activation). Does NOT affect the underlying golden repository or global indexes shared by other users.

DIFFERENCE FROM DELETION:
Deactivation removes YOUR workspace and indexes but keeps the golden repository intact. Deactivated repos can be reactivated later. To permanently delete a golden repository, use remove_golden_repo (admin only).

EXAMPLE:
deactivate_repository(user_alias='my-old-project') removes the activation and frees storage space.
