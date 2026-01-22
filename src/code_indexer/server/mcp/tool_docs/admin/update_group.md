---
name: update_group
category: admin
required_permission: manage_users
tl_dr: Update a custom group name and/or description.
---

TL;DR: Update a custom group name and/or description. Update a custom group's name and/or description. Default groups (admins, powerusers, users) cannot be updated.

INPUTS:
- group_id (required): The unique identifier of the group to update
- name (optional): New group name (must be unique)
- description (optional): New group description

At least one of name or description must be provided.

ERRORS:
- 'Cannot update default groups': Default groups are immutable
- 'Group name already exists': Name must be unique
- 'Group not found': Invalid group_id

EXAMPLE: {"group_id": "grp_abc123", "name": "new-name"} Returns: {"success": true, "group_id": "grp_abc123", "name": "new-name"}