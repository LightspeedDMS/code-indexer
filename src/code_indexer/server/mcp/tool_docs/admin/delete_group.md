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

TL;DR: Delete a custom group (DESTRUCTIVE). Requires MCP elevation (TOTP step-up). Delete a custom group. Default groups (admins, powerusers, users) cannot be deleted. Groups with active members cannot be deleted - reassign users first.

INPUTS:
- group_id (required): The unique identifier of the group to delete

RETURNS:
- success: Boolean indicating if deletion succeeded

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- 'Cannot delete default group': Default groups are protected
- 'Group has active users': Reassign users before deleting
- 'Group not found': Invalid group_id

EXAMPLE: {"group_id": "grp_abc123"} Returns: {"success": true}