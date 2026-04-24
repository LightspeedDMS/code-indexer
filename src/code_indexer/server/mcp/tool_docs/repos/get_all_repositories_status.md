---
name: get_all_repositories_status
category: repos
required_permission: query_repos
tl_dr: Get high-level status summary of ALL repositories (both global and user-activated)
  in one call.
---

TL;DR: Get high-level status summary of ALL repositories (both global and user-activated) in one call. Returns array of repository status summaries. QUICK START: get_all_repositories_status() with no parameters returns all repos. USE CASES: (1) Dashboard overview of system health, (2) Monitor indexing progress across all repos, (3) Identify repos needing attention. OUTPUT: Array of status summaries including alias, activation_status, file_count, last_updated, health indicators. Total count included. SCOPE: Includes both global shared repositories (read-only, '-global' suffix) and your activated repositories (writable, user-specific). NO PARAMETERS: Returns comprehensive list without filtering. COMPARISON: This tool provides overview across all repos. For detailed status of specific repo, use get_repository_status or global_repo_status. TROUBLESHOOTING: Large list? Filter results client-side by activation_status or alias pattern. RELATED TOOLS: get_repository_status (detailed user repo status), global_repo_status (detailed global repo status), get_repository_statistics (comprehensive stats for one repo).