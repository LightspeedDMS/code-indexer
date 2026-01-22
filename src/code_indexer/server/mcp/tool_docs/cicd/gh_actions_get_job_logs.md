---
name: gh_actions_get_job_logs
category: cicd
required_permission: repository:read
tl_dr: Get full log output for a specific job.
---

TL;DR: Get full log output for a specific job. QUICK START: gh_actions_get_job_logs(repository='owner/repo', job_id=67890) returns complete logs. USE CASES: (1) Read full job logs, (2) Debug specific job failure, (3) Analyze job output. RETURNS: Full log output as text. PERMISSIONS: Requires repository:read. EXAMPLE: gh_actions_get_job_logs(repository='owner/repo', job_id=67890)