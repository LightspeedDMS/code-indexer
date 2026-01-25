---
name: get_group
category: admin
required_permission: manage_users
tl_dr: Get detailed information about a specific group.
inputSchema:
  type: object
  properties:
    group_id:
      type: string
      description: The unique identifier of the group to retrieve
  required:
  - group_id
---

TL;DR: Get detailed information about a specific group. Get detailed information about a specific group including its members and accessible repositories. Use this tool to see who belongs to a group and what repositories they can access.

INPUTS:
- group_id (required): The unique identifier of the group

RETURNS:
- id: Group identifier
- name: Group name
- description: Group description
- members: Array of user IDs in the group
- repos: Array of repository names accessible by the group

ERRORS:
- 'Group not found': Invalid group_id

EXAMPLE: {"group_id": "grp_abc123"} Returns: {"success": true, "id": "grp_abc123", "name": "backend-team", "members": ["alice", "bob"], "repos": ["backend-global"]}