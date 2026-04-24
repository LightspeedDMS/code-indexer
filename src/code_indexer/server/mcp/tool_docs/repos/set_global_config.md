---
name: set_global_config
category: repos
required_permission: manage_golden_repos
tl_dr: Configure auto-refresh interval for ALL global repositories system-wide.
---

TL;DR: Configure auto-refresh interval for ALL global repositories system-wide. ADMIN ONLY (requires manage_golden_repos permission). QUICK START: set_global_config(300) sets 5-minute refresh interval. REQUIRED PARAMETER: refresh_interval in seconds (minimum 60, no maximum). EFFECT: All global repositories will automatically pull latest changes and re-index at this interval. TYPICAL VALUES: 300 (5 min, frequent updates), 900 (15 min, balanced), 3600 (1 hour, less load), 86400 (1 day, minimal). TRADEOFFS: Lower intervals = fresher code but higher system load and network usage. Higher intervals = less load but stale code between refreshes. USE CASES: (1) Adjust refresh frequency based on team velocity, (2) Reduce system load during peak hours, (3) Increase update frequency for critical repos. VERIFICATION: Use get_global_config to confirm new setting. SCOPE: Applies to ALL global repos - cannot set per-repo intervals. TROUBLESHOOTING: Permission denied? Requires admin role. Value too low? Must be >= 60 seconds. RELATED TOOLS: get_global_config (check current interval), refresh_golden_repo (force immediate refresh without changing interval), global_repo_status (check when specific repo last refreshed).