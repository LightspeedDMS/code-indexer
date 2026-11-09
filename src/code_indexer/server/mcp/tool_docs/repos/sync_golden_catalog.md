---
name: sync_golden_catalog
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Run the golden catalog sync (GitLab) and/or the agent-workspace registration sync once, as a dry run or for real (async, returns job_id). Requires MCP elevation when enforcement is on.'
slim_description: "[ADMIN ONLY] Sync golden repositories with the configured GitLab groups (adds, alias renames, origin heals; never deletes) and agent workspaces with the content service, once. dry_run (default true) only reports the plan."
inputSchema:
  type: object
  properties:
    dry_run:
      type: boolean
      description: Report the plan only (default true)
    scope:
      type: string
      enum: [catalog, workspaces, all]
      description: Which sync to run (default all)
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the run was accepted
    job_id:
      type: string
      description: Background job ID; its result holds the plan and what was applied
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if refused
  required:
  - success
---

Run the golden catalog sync and/or the workspace registration sync once. Returns a job_id; poll it with get_job_details. The job result holds the plan, and in apply mode what was applied.

CATALOG (scope catalog): The catalog is the projects of the configured GitLab groups plus explicitly listed repositories.
- New projects are added under deterministic shortest-unique path aliases (`team-example-app`).
- `<leaf>-N` aliases are renamed without re-cloning or re-embedding.
- Misassigned rows are renamed and corrected to what the clone holds.
- An origin that was repointed while the content stayed on its clone source is healed.
- Repositories that vanished upstream are reported, never removed.

WORKSPACES (scope workspaces): Live agent workspaces are registered as `ws-*` golden repositories. The list comes from the the content service that provisions them, at CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL. That is not the content service.endpoint, and it never falls back to it; when the URL is unset the pass only reports. A destroyed or absent workspace is deregistered only when the listing is authoritative. A listing error, a non-authoritative listing or an empty listing deregisters nothing. A workspace cidx cannot read is listed as unregistrable with the reason, and is never added. With content_backend='delegated', that means any workspace not provisioned in place at <golden_repos_dir>/<id>.

The scheduled passes run separately, on their own intervals and modes (CIDX_GOLDEN_CATALOG_SYNC_MODE, CIDX_WORKSPACE_SYNC_MODE: off by default). One pass runs at a time across the cluster.

ELEVATION: Matches REST POST /api/admin/golden-catalog/sync. It requires the admin role and, when elevation enforcement is on, an active elevation window.

RELATED TOOLS: get_job_details, rename_golden_repo, add_golden_repo, remove_golden_repo.
