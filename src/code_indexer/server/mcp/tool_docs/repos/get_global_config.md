---
name: get_global_config
category: repos
required_permission: query_repos
tl_dr: Get current auto-refresh interval for ALL global repositories.
---

TL;DR: Get current auto-refresh interval for ALL global repositories. Returns how frequently global repos automatically pull latest changes and re-index. QUICK START: get_global_config() returns current interval. OUTPUT: refresh_interval in seconds (minimum 60). USE CASES: (1) Check current auto-refresh frequency, (2) Audit system configuration, (3) Understand why repos update at certain intervals. SCOPE: This setting applies to ALL global repositories system-wide. Individual repos cannot have different intervals. NO PARAMETERS: Returns global configuration without filtering. TYPICAL VALUES: 300 (5 min), 900 (15 min), 3600 (1 hour). Lower values = fresher code but more system load. RELATED TOOLS: set_global_config (change interval), global_repo_status (check last refresh time for specific repo), refresh_golden_repo (force immediate refresh).