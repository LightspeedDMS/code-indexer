---
name: manage_group_members
category: admin
required_permission: manage_users
tl_dr: Add or remove a user from a group.
slim_description: "Unified group member management: add a user to a group or remove them from a group."
inputSchema:
  type: object
  properties:
    action:
      type: string
      enum:
      - add
      - remove
      description: 'Operation to perform. add: assign user to group. remove: remove user from group.'
    group_id:
      type: string
      description: The unique identifier of the target group
    user_id:
      type: string
      description: The username/ID of the user to add or remove
  required:
  - action
  - group_id
  - user_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Unified group member management (Story #992). Replaces add_member_to_group and remove_member_from_group. Requires MCP elevation (TOTP step-up).

ACTIONS:
- add: Assign user to group. Each user can only belong to one group — this moves them from any prior group.
- remove: Remove user's group membership, leaving them without any group assignment.

INPUTS:
- action (required): 'add' or 'remove'
- group_id (required): The unique identifier of the target group
- user_id (required): The username/ID of the user to add or remove

RETURNS:
- success: Boolean indicating if operation succeeded

ERRORS:
- elevation_required: TOTP step-up needed
- 'Group not found': Invalid group_id
- 'Missing required parameter': Missing user_id

EXAMPLES:
- Add: {"action": "add", "group_id": "grp_abc123", "user_id": "alice"}
- Remove: {"action": "remove", "group_id": "grp_abc123", "user_id": "alice"}
