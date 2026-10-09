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

TL;DR: Add a user to a group or remove them from it. Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`).

ACTIONS:
- add: Assign the user to the group. A user belongs to at most one group, so this moves them out of any prior group. The username must belong to an existing account.
- remove: Remove the user's membership in this group, leaving them without a group. Removing a user who is not in the group changes nothing and returns success.

INPUTS:
- action (required): `add` or `remove`
- group_id (required): Numeric group id as a string (for example `"3"`)
- user_id (required): Username of the user to add or remove

RETURNS:
- success: true when the operation completed

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- `Invalid action '<action>'. Valid actions: [...]`
- `Missing required parameter: group_id` / `Invalid group_id: <value>` (not an integer)
- `Missing required parameter: user_id`
- `Group not found: <id>`
- `User not found: <user_id>` (add)

EXAMPLES:
- Add: {"action": "add", "group_id": "3", "user_id": "example-user"}
- Remove: {"action": "remove", "group_id": "3", "user_id": "example-user"}
