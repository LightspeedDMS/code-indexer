---
name: github_actions_get_run
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific GitHub Actions workflow run.
---

TL;DR: Get detailed information for a specific GitHub Actions workflow run. QUICK START: github_actions_get_run(owner='user', repo='project', run_id=12345) returns detailed run info. USE CASES: (1) Investigate specific workflow run, (2) Get timing and job information, (3) View run artifacts. RETURNS: Detailed run information including jobs with steps, duration, commit SHA, artifacts, html_url. PERMISSIONS: Requires repository:read. EXAMPLE: github_actions_get_run(owner='user', repo='project', run_id=12345)