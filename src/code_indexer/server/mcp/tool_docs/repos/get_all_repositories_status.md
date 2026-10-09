---
name: get_all_repositories_status
category: repos
required_permission: query_repos
tl_dr: Get status summary of ALL repositories (global and user-activated) in one call.
slim_description: "Retrieve a high-level status summary of all repositories (both global shared and user-activated) in one call with no parameters."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repositories:
      type: array
      description: Array of repository status summaries
    total:
      type: integer
      description: Total number of repositories
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Get high-level status summary of global and user-activated repositories in one call. QUICK START: get_all_repositories_status() with no parameters. USE CASES: (1) Overview of the repositories available to you, (2) Identify repos needing attention.

OUTPUT: `{"success": true, "repositories": [...], "total": N}` with two kinds of entries:
- Activated entries: for each of your activations, the details of the golden repository whose alias equals the activation's `user_alias`: `alias`, `repo_url`, `default_branch`, `clone_path`, `created_at`, `activation_status` (activated/available), `branches_list`, `file_count`, `index_size`, `last_updated`. An activation whose `user_alias` differs from every golden repository alias (for example a custom alias such as `my-work`) is omitted from the list; use list_repositories to see every activation.
- Global entries: `user_alias` (the `-global` alias), `golden_repo_alias`, `current_branch: null`, `is_global: true`, `repo_url`, `last_refresh`, `index_path`, `created_at`. When group-based access control is configured, only repositories the caller's groups can access are included.

MEMORY PRESSURE: When the server is under memory pressure the call is refused with `{"success": false, "error_code": "memory_pressure", "error": "Server is under high memory pressure; please retry shortly.", "retry_after_seconds": N}`. Other failures return `{"success": false, "error": "...", "repositories": [], "total": 0}`.

RELATED TOOLS: repository_status (detailed status of one global or activated repository), list_repositories (all your activations plus global repos), list_global_repos (global repositories only).
