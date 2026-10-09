---
name: delete_group
category: admin
required_permission: manage_users
tl_dr: Delete a custom group (DESTRUCTIVE).
slim_description: "Delete a custom group by group_id (requires manage_users)."
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group to delete
  required:
  - group_id
---

TL;DR: Delete a custom group (DESTRUCTIVE). Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`). Default groups (admins, powerusers, users) cannot be deleted, and a group that still has members cannot be deleted: move or remove its members first (`manage_group_members`). The group's repository grants are deleted with it.

INPUTS:
- group_id (required): Numeric group id as a string (for example `"4"`)

RETURNS:
- success: true when the group was deleted

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- `Missing required parameter: group_id` / `Invalid group_id: <value>` (not an integer)
- `Group not found: <id>`
- `Cannot delete default group: <name>`
- `Cannot delete group with <N> active user(s)`

EXAMPLE: {"group_id": "4"} Returns: {"success": true}
