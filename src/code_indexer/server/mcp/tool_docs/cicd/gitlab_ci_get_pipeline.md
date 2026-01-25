---
name: gitlab_ci_get_pipeline
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific GitLab CI pipeline.
inputSchema:
  type: object
  properties:
    project_id:
      type: string
      description: Project in 'namespace/project' format or numeric ID
    pipeline_id:
      type: integer
      description: Pipeline ID
    base_url:
      type: string
      description: 'Optional GitLab instance base URL (default: https://gitlab.com)'
  required:
  - project_id
  - pipeline_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    pipeline:
      type: object
      properties:
        id:
          type: integer
        status:
          type: string
        ref:
          type: string
        sha:
          type: string
        created_at:
          type: string
        updated_at:
          type: string
        web_url:
          type: string
        duration:
          type: integer
        coverage:
          type: string
        jobs:
          type: array
---

TL;DR: Get detailed information for a specific GitLab CI pipeline. QUICK START: gitlab_ci_get_pipeline(project_id='namespace/project', pipeline_id=12345) returns detailed pipeline info. USE CASES: (1) Investigate specific pipeline run, (2) Get timing information, (3) View jobs and stages. RETURNS: Detailed pipeline information including jobs, duration, coverage, commit SHA. PERMISSIONS: Requires repository:read. EXAMPLE: gitlab_ci_get_pipeline(project_id='namespace/project', pipeline_id=12345)