---
name: create_user
category: admin
required_permission: manage_users
tl_dr: Create new user account with specified username, password, and role.
---

TL;DR: Create new user account with specified username, password, and role. ADMIN ONLY (requires manage_users permission). QUICK START: create_user('alice', 'secure_password', 'power_user') creates power user. REQUIRED FIELDS: username (unique identifier), password (stored securely), role (admin/power_user/normal_user). ROLE SELECTION: Choose based on needed permissions - normal_user (query only), power_user (activate repos + write files + query), admin (full access including user/repo management). SECURITY: Passwords are hashed before storage. Username must be unique. USE CASES: (1) Onboard new team members, (2) Create service accounts for automation, (3) Grant appropriate access levels. VERIFICATION: Use list_users to confirm user creation. User can immediately authenticate with credentials. TROUBLESHOOTING: Username exists? Must be unique across system. Permission denied? Requires admin role. RELATED TOOLS: list_users (verify creation), authenticate (test login).