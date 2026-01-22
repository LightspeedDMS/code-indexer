---
name: remove_member_from_group
category: admin
required_permission: manage_users
tl_dr: Remove a user from a group.
---

Remove a user from a group. This removes the user's group membership entirely, leaving them without any group assignment.

INPUTS:
- group_id (required): The unique identifier of the group
- user_id (required): The username/ID of the user to remove

RETURNS:
- success: Boolean indicating if removal succeeded

ERRORS:
- 'Group not found': Invalid group_id