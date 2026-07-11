---
name: update_group
category: admin
required_permission: manage_users
tl_dr: Update a custom group name and/or description.
slim_description: "Update a custom group's name and/or description by group_id (requires manage_users)."
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group to update
    name:
      type: string
      description: New group name (optional)
    description:
      type: string
      description: New group description (optional)
  required:
  - group_id
---

TL;DR: Update a custom group name and/or description. Requires MCP elevation (TOTP step-up). Update a custom group's name and/or description. Default groups (admins, powerusers, users) cannot be updated.

INPUTS:
- group_id (required): The unique identifier of the group to update
- name (optional): New group name (must be unique)
- description (optional): New group description

At least one of name or description must be provided.

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- 'Cannot update default groups': Default groups are immutable
- 'Group name already exists': Name must be unique
- 'Group not found': Invalid group_id

EXAMPLE: {"group_id": "grp_abc123", "name": "new-name"} Returns: {"success": true}