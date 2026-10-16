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

TL;DR: Grant or revoke a group's access to repositories. Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`).

ACTIONS:
- add: Grant the group access to every name in `repos`. Names the group can already access are not counted; `added_count` reports the new grants.
- remove: Revoke access to one repository. The name comes from `repo_name`; when `repo_name` is absent, the first element of the `repos` array is used and any further elements are ignored. cidx-meta cannot be revoked.
- bulk_remove: Revoke access to every name in `repos`. cidx-meta is skipped without an error; `removed_count` reports the revocations made.

INPUTS:
- action (required): `add`, `remove` or `bulk_remove`
- group_id (required): Numeric group id as a string (for example `"3"`), as returned by `list_groups` or `create_group`
- repos (add, bulk_remove; or remove when repo_name is absent): Array of repository names. For add and bulk_remove a JSON-encoded array string such as `'["example-repo"]'` is also accepted; for remove pass a real array or use `repo_name`.
- repo_name (remove): Single repository name

Repository names are golden repository aliases without the `-global` suffix.

RETURNS:
- add: `{"success": true, "added_count": N}`
- remove: `{"success": true}`
- bulk_remove: `{"success": true, "removed_count": N}`

ERRORS (all returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- `Invalid action '<action>'. Valid actions: [...]`
- `Missing required parameter: group_id` / `Invalid group_id: <value>` (not an integer)
- `Group not found: <id>`
- `Missing required parameter: repo_names` (add or bulk_remove without `repos`) / `Missing required parameter: repo_name` (remove without a name)
- `Repository '<name>' not found in group's access list` (remove)
- `cidx-meta access cannot be revoked from any group` (remove)

EXAMPLES:
- Add: {"action": "add", "group_id": "3", "repos": ["example-repo", "other-repo"]}
- Remove: {"action": "remove", "group_id": "3", "repo_name": "example-repo"}
- Bulk remove: {"action": "bulk_remove", "group_id": "3", "repos": ["example-repo", "other-repo"]}
