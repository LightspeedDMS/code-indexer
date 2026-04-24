---
name: git_fetch
category: git
required_permission: repository:write
tl_dr: Download remote changes without merging.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    remote:
      type: string
      description: 'Remote name (default: origin)'
      default: origin
  required:
  - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    remote:
      type: string
      description: Remote name
    refs_fetched:
      type: array
      items:
        type: string
      description: List of refs/branches fetched
---

TL;DR: Download remote changes without merging. Fetch changes from remote repository without merging. USE CASES: (1) Download remote updates, (2) Check remote changes before merge, (3) Update remote-tracking branches. OPTIONAL: Specify remote (default: origin). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "remote": "origin"} Returns: {"success": true, "remote": "origin", "refs_fetched": ["refs/heads/main", "refs/heads/develop"]}