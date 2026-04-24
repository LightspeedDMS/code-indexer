---
name: refresh_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: Update global repo by pulling latest changes from git remote and re-indexing.
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Repository alias
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

TL;DR: Update global repository by pulling latest changes from git remote and re-indexing. Synchronizes global repo with upstream repository. ADMIN ONLY (requires manage_golden_repos permission). QUICK START: refresh_golden_repo('backend-global') pulls latest and re-indexes. WHAT IT DOES: (1) Git pull from remote origin, (2) Re-index all new/changed files, (3) Update search indexes with latest code. BACKGROUND JOB: Returns job_id for async operation - refresh can take minutes for large repos. Use get_job_details to monitor. AUTOMATIC REFRESH: Global repos also have auto-refresh configured via get_global_config/set_global_config (minimum 60s interval). This tool triggers manual on-demand refresh. USE CASES: (1) Get latest code changes immediately without waiting for auto-refresh, (2) Refresh after known upstream changes, (3) Force re-index after issues. VERIFICATION: Check global_repo_status after job completes - last_refreshed timestamp should update. RELATED TOOLS: global_repo_status (check last refresh time), get_job_details (monitor refresh job), set_global_config (configure auto-refresh interval).