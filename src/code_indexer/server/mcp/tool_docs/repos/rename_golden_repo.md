---
name: rename_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Rename a golden repository alias without re-cloning or re-embedding (async, returns job_id). Requires MCP elevation when enforcement is on.'
slim_description: "[ADMIN ONLY] Rename a golden repository alias (clone, published index, -global pointer, registry rows and references move; nothing is re-indexed). Optionally corrects a mis-registered URL/branch. Requires MCP elevation (TOTP step-up) when enforcement is on."
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Current golden repository alias (without -global suffix)
    new_alias:
      type: string
      description: New alias (letters, digits, '-' and '_'; must not exist)
    repo_url:
      type: string
      description: Optional corrected repository URL; must be the clone's origin (a file:// URL must name the renamed directory)
    branch:
      type: string
      description: Optional corrected branch; must be the clone's checked-out branch, on origin's history
  required:
  - alias
  - new_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the rename was accepted
    job_id:
      type: string
      description: Background job ID; poll with get_job_details
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if refused
  required:
  - success
---

Rename a golden repository's alias. Returns at once with a job_id; poll it with get_job_details.

PARAMETERS: alias (required), new_alias (required), repo_url and branch (optional). Use repo_url and branch to correct a row that names something other than what the clone holds. They must match the clone's origin and HEAD, or the rename is refused.

BEHAVIOR: The job holds both aliases' write locks. It refuses while a refresh, add, removal, branch change or rename of either alias is in flight, or while the old alias is in write mode. It then moves:
- the clone directory;
- the published index, copied to the new alias's store name (an upload, not a re-index);
- the -global pointer and the registry rows;
- activated and composite repository references;
- group grants;
- the cidx-meta description;
- the wiki cache and the dependency-map tracking.

The old .versioned snapshots stay where the pointer names them, and the next refresh snapshots under the new name. A rename interrupted part-way resumes when it is run again with the same aliases.

NOT KEPT: The old alias does not redirect. Callers must switch to the new alias (`<new_alias>-global` for queries). Job history, audit records and statistics keep the old alias.

ERRORS: An invalid alias, or a URL or branch that does not match the clone. Unknown alias. New alias already registered. Alias busy (an in-flight job, a refresh, or write mode). Maintenance mode.

ELEVATION: Matches REST POST /api/admin/golden-repos/{alias}/rename. It requires the admin role and, when elevation enforcement is on, an active elevation window. Call `elevate_session` first.

RELATED TOOLS: get_job_details, remove_golden_repo, change_golden_repo_branch, list_global_repos.
