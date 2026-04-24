---
name: bulk_remove_repos_from_group
category: admin
required_permission: manage_users
tl_dr: Revoke a group's access to multiple repositories in a single operation.
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group
    repo_names:
      type: array
      items:
        type: string
      description: Array of repository names to revoke access from
  required:
  - group_id
  - repo_names
---

Revoke a group's access to multiple repositories in a single operation. Users in the group will no longer be able to query these repositories.

INPUTS:
- group_id (required): The unique identifier of the group
- repo_names (required): Array of repository names to revoke access from

RETURNS:
- success: Boolean indicating if operation succeeded
- removed_count: Number of repositories actually removed

NOTE: cidx-meta is silently skipped (cannot be removed).

ERRORS:
- 'Group not found': Invalid group_id