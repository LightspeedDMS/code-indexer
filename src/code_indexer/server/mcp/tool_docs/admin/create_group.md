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
      description: Unique group name (1-100 characters)
    description:
      type: string
      description: Optional group description
  required:
  - name
---

TL;DR: Create a new custom group for organizing users and repository access. Requires MCP elevation (TOTP step-up). Custom groups can be assigned users and granted access to specific repositories. Default groups (admins, powerusers, users) cannot be created - they exist automatically.

INPUTS:
- name (required): Unique group name (1-100 chars, alphanumeric with hyphens/underscores)
- description (optional): Description of the group's purpose

RETURNS:
- group_id: ID of the newly created group
- name: Name of the created group

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- 'Group name already exists': Name must be unique
- 'Invalid group name': Name contains invalid characters

EXAMPLE: {"name": "backend-team", "description": "Backend developers"} Returns: {"success": true, "group_id": "grp_abc123", "name": "backend-team"}