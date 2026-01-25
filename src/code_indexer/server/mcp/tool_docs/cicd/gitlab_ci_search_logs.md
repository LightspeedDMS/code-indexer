---
name: gitlab_ci_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search GitLab CI pipeline logs for a pattern using regex matching.
inputSchema:
  type: object
  properties:
    project_id:
      type: string
      description: Project in 'namespace/project' format or numeric ID
    pipeline_id:
      type: integer
      description: Pipeline ID
    pattern:
      type: string
      description: Regex pattern to search for (case-insensitive)
    base_url:
      type: string
      description: 'Optional GitLab instance base URL (default: https://gitlab.com)'
  required:
  - project_id
  - pipeline_id
  - pattern
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    matches:
      type: array
      items:
        type: object
        properties:
          job_id:
            type: integer
          job_name:
            type: string
          stage:
            type: string
          line:
            type: string
          line_number:
            type: integer
---

TL;DR: Search GitLab CI pipeline logs for a pattern using regex matching. QUICK START: gitlab_ci_search_logs(project_id='namespace/project', pipeline_id=12345, pattern='error') finds errors in logs. USE CASES: (1) Find error messages in logs, (2) Search for specific patterns, (3) Debug pipeline failures. RETURNS: List of matching log lines with job_id, job_name, stage, line, line_number. PERMISSIONS: Requires repository:read. EXAMPLE: gitlab_ci_search_logs(project_id='namespace/project', pipeline_id=12345, pattern='ERROR|FAIL')