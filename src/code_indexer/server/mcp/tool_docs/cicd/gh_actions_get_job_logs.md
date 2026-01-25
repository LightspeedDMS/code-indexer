---
name: gh_actions_get_job_logs
category: cicd
required_permission: repository:read
tl_dr: Get full log output for a specific job.
inputSchema:
  type: object
  properties:
    repository:
      type: string
      description: Repository in 'owner/repo' format
    job_id:
      type: integer
      description: Job ID
  required:
  - repository
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

TL;DR: Get full log output for a specific job. QUICK START: gh_actions_get_job_logs(repository='owner/repo', job_id=67890) returns complete logs. USE CASES: (1) Read full job logs, (2) Debug specific job failure, (3) Analyze job output. RETURNS: Full log output as text. PERMISSIONS: Requires repository:read. EXAMPLE: gh_actions_get_job_logs(repository='owner/repo', job_id=67890)