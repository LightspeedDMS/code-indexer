---
name: add_member_to_group
category: admin
required_permission: manage_users
tl_dr: Assign a user to a group.
---

TL;DR: Assign a user to a group. Assign a user to a group. Each user can only belong to one group at a time - this operation will move the user from their current group to the specified group.

INPUTS:
- group_id (required): The unique identifier of the target group
- user_id (required): The username/ID of the user to assign

RETURNS:
- success: Boolean indicating if assignment succeeded

ERRORS:
- 'Group not found': Invalid group_id
- 'User not found': Invalid user_id

EXAMPLE: {"group_id": "grp_abc123", "user_id": "alice"} Returns: {"success": true}