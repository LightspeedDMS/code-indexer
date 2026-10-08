---
name: refresh_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Update global repo by pulling latest changes from git remote and re-indexing. Requires MCP elevation when enforcement is on.'
slim_description: "[ADMIN ONLY] Force an immediate git pull from remote origin and re-indexing of a global repository identified by alias. Requires MCP elevation (TOTP step-up) when enforcement is on."
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Golden repository alias without the -global suffix (e.g. 'example-repo')
  required:
  - alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type:
      - string
      - 'null'
      description: Background job ID
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Update global repository by pulling latest changes from git remote and re-indexing. Synchronizes global repo with upstream repository. ADMIN ONLY (requires manage_golden_repos permission). QUICK START: {"alias": "example-repo"} pulls latest and re-indexes the repository served as `example-repo-global`. ALIAS FORMAT: Pass the golden repository alias WITHOUT the '-global' suffix (the `repo_name` field of list_global_repos); a '-global' alias is reported as `Golden repository '<alias>' not found`. WHAT IT DOES: (1) Git pull from remote origin, (2) Re-index all new/changed files, (3) Update search indexes with latest code. BACKGROUND JOB: Returns job_id for async operation - refresh can take minutes for large repos. Use get_job_details to monitor. AUTOMATIC REFRESH: Global repos also have auto-refresh configured via get_global_config/set_global_config (minimum 60s interval). This tool triggers manual on-demand refresh. USE CASES: (1) Get latest code changes immediately without waiting for auto-refresh, (2) Refresh after known upstream changes, (3) Force re-index after issues. RETURNS: `{"success": true, "job_id": "...", "message": "Golden repository '<alias>' refresh started"}`; errors return `{"success": false, "error": "...", "job_id": null}` (for example `Missing required parameter: alias`, `Golden repository '<alias>' not found`, `RefreshScheduler not available`). VERIFICATION: After the job completes, the repository's `last_refresh` in list_global_repos is updated. RELATED TOOLS: repository_status (check last refresh time), get_job_details (monitor refresh job), set_global_config (configure auto-refresh interval).

ELEVATION: Matches the REST (POST /api/admin/golden-repos/{alias}/refresh) and Web twins: requires the admin role and, when elevation enforcement is on, an active elevation window (call `elevate_session` first; MCP-credential and OAuth callers are elevated automatically).

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
