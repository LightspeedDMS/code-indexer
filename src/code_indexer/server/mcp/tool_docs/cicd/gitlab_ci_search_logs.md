---
name: gitlab_ci_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search GitLab CI pipeline logs for a pattern using regex matching.
---

TL;DR: Search GitLab CI pipeline logs for a pattern using regex matching. QUICK START: gitlab_ci_search_logs(project_id='namespace/project', pipeline_id=12345, pattern='error') finds errors in logs. USE CASES: (1) Find error messages in logs, (2) Search for specific patterns, (3) Debug pipeline failures. RETURNS: List of matching log lines with job_id, job_name, stage, line, line_number. PERMISSIONS: Requires repository:read. EXAMPLE: gitlab_ci_search_logs(project_id='namespace/project', pipeline_id=12345, pattern='ERROR|FAIL')