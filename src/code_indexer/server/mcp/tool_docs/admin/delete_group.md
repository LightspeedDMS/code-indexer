---
name: delete_group
category: admin
required_permission: manage_users
tl_dr: Delete a custom group (DESTRUCTIVE).
---

TL;DR: Delete a custom group (DESTRUCTIVE). Delete a custom group. Default groups (admins, powerusers, users) cannot be deleted. Groups with active members cannot be deleted - reassign users first.

INPUTS:
- group_id (required): The unique identifier of the group to delete

RETURNS:
- success: Boolean indicating if deletion succeeded

ERRORS:
- 'Cannot delete default group': Default groups are protected
- 'Group has active users': Reassign users before deleting
- 'Group not found': Invalid group_id

EXAMPLE: {"group_id": "grp_abc123"} Returns: {"success": true}