---
name: add_repos_to_group
category: admin
required_permission: manage_users
tl_dr: Grant a group access to one or more repositories.
---

Grant a group access to one or more repositories. Users in the group will be able to query these repositories.

INPUTS:
- group_id (required): The unique identifier of the group
- repo_names (required): Array of repository names to grant access to

RETURNS:
- success: Boolean indicating if operation succeeded
- added_count: Number of repositories newly added (repos already accessible are skipped)

IDEMPOTENT: Adding repos that are already accessible is a no-op.

ERRORS:
- 'Group not found': Invalid group_id