---
name: list_groups
category: admin
required_permission: manage_users
tl_dr: List all groups with member counts and repository access information.
---

List all groups with member counts and repository access information. Returns the default groups (admins, powerusers, users) and any custom groups. Use this tool to see what groups exist and their basic statistics before performing group management operations.

RESPONSE FIELDS:
- id: Unique group identifier
- name: Group name
- description: Group description
- member_count: Number of users in the group
- repo_count: Number of repositories accessible by the group

EXAMPLE:
list_groups() -> Returns all groups with counts