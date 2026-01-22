---
name: global_repo_status
category: repos
required_permission: query_repos
tl_dr: Get detailed status of specific GLOBAL repository (shared, read-only) including
  refresh timestamps and temporal indexing capabilities.
---

TL;DR: Get detailed status of specific GLOBAL repository (shared, read-only) including refresh timestamps and temporal indexing capabilities. ALIAS REQUIREMENT: Use full '-global' suffix alias (e.g., 'backend-global'). QUICK START: global_repo_status('backend-global') returns global repo status. OUTPUT FIELDS: alias, repo_name, url (git repository URL), last_refresh (ISO 8601 timestamp), enable_temporal (boolean indicating git history search support). USE CASES: (1) Check when global repo was last refreshed, (2) Verify temporal search availability before time-range queries, (3) Confirm repository URL and configuration. TEMPORAL STATUS: If enable_temporal=true, can use time_range/at_commit parameters in search_code. If false, temporal queries return empty results. COMPARISON: global_repo_status (global shared repos) vs get_repository_status (your activated repos). TROUBLESHOOTING: Repository not found? Verify alias with list_global_repos. Want to force refresh? Use refresh_golden_repo. RELATED TOOLS: list_global_repos (see all global repos), refresh_golden_repo (update repo), get_global_config (check auto-refresh interval), search_code with temporal params (use temporal index).