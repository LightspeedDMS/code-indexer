---
name: list_users
category: admin
required_permission: manage_users
tl_dr: List all users in CIDX system with roles and creation timestamps.
---

TL;DR: List all users in CIDX system with roles and creation timestamps. ADMIN ONLY (requires manage_users permission). QUICK START: list_users() with no parameters returns all users. OUTPUT FIELDS: Each user includes username, role (admin/power_user/normal_user), created_at (ISO 8601 timestamp). Total count included. ROLE TYPES: admin (full access), power_user (can activate repos and write files), normal_user (read-only query access). USE CASES: (1) Audit user accounts, (2) Check user roles before granting permissions, (3) Monitor user growth. NO PARAMETERS: Returns all users without filtering. TROUBLESHOOTING: Permission denied? Requires admin role with manage_users permission. RELATED TOOLS: create_user (add new user), authenticate (login).