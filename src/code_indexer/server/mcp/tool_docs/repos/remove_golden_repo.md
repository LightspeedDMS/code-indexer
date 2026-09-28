---
name: remove_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Remove global shared repository from CIDX server. Requires MCP elevation when enforcement is on.'
slim_description: "[ADMIN ONLY] Permanently remove a global shared repository, its indexes, and all associated storage for all users. Requires MCP elevation (TOTP step-up) when enforcement is on."
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

TL;DR: Remove global shared repository from CIDX server. Deletes repository from golden repos list, removes indexes, and cleans up storage. ADMIN ONLY (requires manage_golden_repos permission). QUICK START: remove_golden_repo('backend-global') removes the global repository. DESTRUCTIVE OPERATION: This permanently removes the repository and all associated indexes for ALL users. Global repos serve all users, so removal affects everyone. BACKGROUND JOB: Returns job_id for async operation - use get_job_details to monitor progress. USE CASES: (1) Decommission deprecated repositories, (2) Clean up test repositories, (3) Free storage space. VERIFICATION: Use list_global_repos to confirm removal. ALIAS FORMAT: Provide the full alias including '-global' suffix. TROUBLESHOOTING: Permission denied? Requires admin role. Repository not found? Verify alias with list_global_repos. RELATED TOOLS: add_golden_repo (add new global repo), list_global_repos (see all global repos), get_job_details (monitor removal job).

ELEVATION: Matches the REST (DELETE /api/admin/golden-repos/{alias}) and Web twins: requires the admin role and, when elevation enforcement is on, an active elevation window (call `elevate_session` first; MCP-credential and OAuth callers are elevated automatically).

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)