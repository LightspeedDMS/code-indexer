---
name: github_actions_get_job_logs
category: cicd
required_permission: repository:read
tl_dr: Get full log output for a specific GitHub Actions job.
inputSchema:
  type: object
  properties:
    owner:
      type: string
      description: Repository owner
    repo:
      type: string
      description: Repository name
    job_id:
      type: integer
      description: Job ID
  required:
  - owner
  - repo
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

TL;DR: Get full log output for a specific GitHub Actions job. QUICK START: github_actions_get_job_logs(owner='user', repo='project', job_id=67890) returns complete logs. USE CASES: (1) Read full job logs, (2) Debug specific job failure, (3) Analyze job output. RETURNS: Full log output as text. PERMISSIONS: Requires repository:read. EXAMPLE: github_actions_get_job_logs(owner='user', repo='project', job_id=67890)