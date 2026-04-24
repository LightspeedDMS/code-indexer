---
name: gitlab_ci_get_job_logs
category: cicd
required_permission: repository:read
tl_dr: Get full log output for a specific GitLab CI job.
inputSchema:
  type: object
  properties:
    project_id:
      type: string
      description: Project in 'namespace/project' format or numeric ID
    job_id:
      type: integer
      description: Job ID
    base_url:
      type: string
      description: 'Optional GitLab instance base URL (default: https://gitlab.com)'
  required:
  - project_id
  - job_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    logs:
      type: string
      description: Full log output
---

TL;DR: Get full log output for a specific GitLab CI job. QUICK START: gitlab_ci_get_job_logs(project_id='namespace/project', job_id=67890) returns complete logs. USE CASES: (1) Read full job logs, (2) Debug specific job failure, (3) Analyze job output. RETURNS: Full log output as text. PERMISSIONS: Requires repository:read. EXAMPLE: gitlab_ci_get_job_logs(project_id='namespace/project', job_id=67890)