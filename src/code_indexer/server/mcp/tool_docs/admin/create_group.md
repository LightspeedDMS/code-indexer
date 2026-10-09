---
name: create_group
category: admin
required_permission: manage_users
tl_dr: Create a new custom group for organizing users and repository access.
slim_description: "Create a custom group with a unique name and optional description (requires manage_users)."
inputSchema:
  type: object
  properties:
    name:
      type: string
      description: Group name; must be unique, compared case-insensitively
    description:
      type: string
      description: Optional group description
  required:
  - name
---

TL;DR: Create a new custom group for organizing users and repository access. Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`). Custom groups can be assigned users (`manage_group_members`) and granted repositories (`manage_group_repos`). The default groups (admins, powerusers, users) always exist and are not created with this tool.

INPUTS:
- name (required): Group name. It must not match an existing group name, ignoring case.
- description (optional): Description of the group's purpose

RETURNS:
- group_id: Integer id of the new group. Pass it as a string (for example `"4"`) to the other group tools.
- name: Name of the created group

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on; `totp_setup_required` includes `setup_url`)
- `Missing required parameter: name`
- `Group with name '<name>' already exists`

EXAMPLE: {"name": "backend-team", "description": "Backend developers"} Returns: {"success": true, "group_id": 4, "name": "backend-team"}
