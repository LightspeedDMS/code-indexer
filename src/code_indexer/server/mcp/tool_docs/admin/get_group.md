---
name: get_group
category: admin
required_permission: manage_users
tl_dr: Get detailed information about a specific group.
slim_description: "Get detailed information about a specific group by group_id (requires manage_users)."
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group to retrieve
  required:
  - group_id
---

TL;DR: Get a group's details, including its members and the repositories it can access. Requires the admin role.

INPUTS:
- group_id (required): Numeric group id as a string (for example `"3"`), as returned by `list_groups`

RETURNS:
- id: Integer group id
- name: Group name
- description: Group description
- members: Array of usernames in the group
- repos: Array of repository names the group can access; cidx-meta is always listed first

ERRORS (returned as `{"success": false, "error": "..."}`):
- `Permission denied: admin role required`
- `Missing required parameter: group_id` / `Invalid group_id: <value>` (not an integer)
- `Group not found: <id>`

EXAMPLE: {"group_id": "3"} Returns: {"success": true, "id": 3, "name": "backend-team", "description": "Backend developers", "members": ["example-user"], "repos": ["cidx-meta", "example-repo"]}
