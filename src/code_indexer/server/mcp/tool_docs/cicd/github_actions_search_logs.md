---
name: github_actions_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search GitHub Actions workflow run logs for a pattern using regex matching.
---

TL;DR: Search GitHub Actions workflow run logs for a pattern using regex matching. QUICK START: github_actions_search_logs(owner='user', repo='project', run_id=12345, query='error') finds errors in logs. USE CASES: (1) Find error messages in logs, (2) Search for specific patterns, (3) Debug workflow failures. RETURNS: List of matching log lines with job_id, job_name, line, line_number. PERMISSIONS: Requires repository:read. EXAMPLE: github_actions_search_logs(owner='user', repo='project', run_id=12345, query='ERROR|FAIL')