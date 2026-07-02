---
name: manage_group_repos
category: admin
required_permission: manage_users
tl_dr: Add or remove repository access for a group.
slim_description: "Unified group repo management: grant repos to a group, revoke a single repo, or bulk-revoke multiple repos."
inputSchema:
  type: object
  properties:
    action:
      type: string
      enum:
      - add
      - remove
      - bulk_remove
      description: 'Operation to perform. add: grant repo access. remove: revoke single repo. bulk_remove: revoke multiple repos.'
    group_id:
      type: string
      description: The unique identifier of the group
    repos:
      type: array
      items:
        type: string
      description: Array of repository names. Used for add and bulk_remove. For remove, provide a single-element list.
    repo_name:
      type: string
      description: Single repository name (alternative to repos list for remove action).
  required:
  - action
  - group_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    added_count:
      type: integer
      description: Number of repositories newly granted (add action)
    removed_count:
      type: integer
      description: Number of repositories revoked (bulk_remove action)
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Unified group repo management (Story #992). Replaces add_repos_to_group, remove_repo_from_group, and bulk_remove_repos_from_group. Requires MCP elevation (TOTP step-up).

ACTIONS:
- add: Grant group access to one or more repositories. Idempotent — already-accessible repos are skipped.
- remove: Revoke access to a single repository. cidx-meta cannot be revoked.
- bulk_remove: Revoke access to multiple repositories. cidx-meta is silently skipped.

INPUTS:
- action (required): 'add', 'remove', or 'bulk_remove'
- group_id (required): The unique identifier of the group
- repos (add/bulk_remove): Array of repository names
- repo_name (remove): Single repository name (or provide repos=[name])

RETURNS:
- success: Boolean
- added_count (add): Number of newly granted repos
- removed_count (bulk_remove): Number of repos actually removed

ERRORS:
- elevation_required: TOTP step-up needed
- 'Group not found': Invalid group_id
- 'cidx-meta access cannot be revoked': Attempt to revoke the protected repo

EXAMPLES:
- Add: {"action": "add", "group_id": "grp_abc123", "repos": ["svc-a", "svc-b"]}
- Remove: {"action": "remove", "group_id": "grp_abc123", "repo_name": "svc-a"}
- Bulk remove: {"action": "bulk_remove", "group_id": "grp_abc123", "repos": ["svc-a", "svc-b"]}
