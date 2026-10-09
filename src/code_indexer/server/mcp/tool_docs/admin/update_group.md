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

TL;DR: Update a custom group's name and/or description. Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`). Default groups (admins, powerusers, users) cannot be updated.

INPUTS:
- group_id (required): Numeric group id as a string (for example `"4"`)
- name (optional): New group name; must not match another group's name, ignoring case
- description (optional): New group description

Fields that are omitted are left unchanged; a call with neither field changes nothing and returns success.

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- `Missing required parameter: group_id` / `Invalid group_id: <value>` (not an integer)
- `Group not found: <id>`
- `Cannot update default groups`
- `Group with name '<name>' already exists`

EXAMPLE: {"group_id": "4", "name": "platform-team"} Returns: {"success": true}
