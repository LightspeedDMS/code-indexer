---
name: create_user
category: admin
required_permission: manage_users
tl_dr: Create new user account with specified username, password, and role.
slim_description: "Create a new user account with username, password, and role (admin/power_user/normal_user), requiring manage_users permission."
inputSchema:
  type: object
  properties:
    username:
      type: string
      description: Username
    password:
      type: string
      description: Password
    role:
      type: string
      description: User role
      enum:
      - admin
      - power_user
      - normal_user
  required:
  - username
  - password
  - role
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    user:
      type:
      - object
      - 'null'
      description: Created user information
      properties:
        username:
          type: string
          description: Username
        role:
          type: string
          description: User role
        created_at:
          type: string
          description: ISO 8601 creation timestamp
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Create new user account with specified username, password, and role. ADMIN ONLY: requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`). QUICK START: {"username": "example-user", "password": "<password>", "role": "power_user"} creates a power user. REQUIRED FIELDS: username (unique identifier), password (stored hashed), role (admin/power_user/normal_user). ROLE SELECTION: normal_user (query, activate personal workspaces, switch branches on and sync own workspace; cannot write files or manage users/golden repos), power_user (normal_user plus file and git write operations), admin (full access including user and golden repository management). DEFAULT GROUP: The new user is added to the `admins` group (role admin) or the `users` group (other roles); if that assignment fails, the account is still created and the failure is logged on the server. USE CASES: (1) Onboard new team members, (2) Create service accounts for automation, (3) Grant appropriate access levels. RETURNS: {"success": true, "user": {"username", "role", "created_at"}, "message": "User '<username>' created successfully"}. VERIFICATION: Use list_users to confirm user creation.

ERRORS (returned as `{"success": false, "error": "...", "user": null}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- Username already exists, invalid role, or a missing field: the error text describes the cause

RELATED TOOLS: list_users (verify creation), manage_group_members (move the user to another group).
