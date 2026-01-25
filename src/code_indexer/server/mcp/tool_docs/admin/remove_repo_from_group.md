---
name: remove_repo_from_group
category: admin
required_permission: manage_users
tl_dr: Revoke a group's access to a single repository.
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group
    repo_name:
      type: string
      description: The repository name to revoke access from
  required:
  - group_id
  - repo_name
---

Revoke a group's access to a single repository. Users in the group will no longer be able to query this repository.

INPUTS:
- group_id (required): The unique identifier of the group
- repo_name (required): The repository name to revoke access from

RETURNS:
- success: Boolean indicating if revocation succeeded

NOTE: cidx-meta access cannot be revoked from any group.

ERRORS:
- 'Group not found': Invalid group_id
- 'Repository access not found': Repo was not in group's access list
- 'cidx-meta access cannot be revoked': Special repository is always accessible