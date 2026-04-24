---
name: create_user
category: admin
required_permission: manage_users
tl_dr: Create new user account with specified username, password, and role.
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

TL;DR: Create new user account with specified username, password, and role. ADMIN ONLY (requires manage_users permission). QUICK START: create_user('alice', 'secure_password', 'power_user') creates power user. REQUIRED FIELDS: username (unique identifier), password (stored securely), role (admin/power_user/normal_user). ROLE SELECTION: Choose based on needed permissions - normal_user (query only), power_user (activate repos + write files + query), admin (full access including user/repo management). SECURITY: Passwords are hashed before storage. Username must be unique. USE CASES: (1) Onboard new team members, (2) Create service accounts for automation, (3) Grant appropriate access levels. VERIFICATION: Use list_users to confirm user creation. User can immediately authenticate with credentials. TROUBLESHOOTING: Username exists? Must be unique across system. Permission denied? Requires admin role. RELATED TOOLS: list_users (verify creation), authenticate (test login).