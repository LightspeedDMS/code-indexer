---
name: gitlab_ci_cancel_pipeline
category: cicd
required_permission: repository:write
tl_dr: Cancel a running GitLab CI pipeline.
inputSchema:
  type: object
  properties:
    project_id:
      type: string
      description: Project in 'namespace/project' format or numeric ID
    pipeline_id:
      type: integer
      description: Pipeline ID to cancel
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
    pipeline_id:
      type: integer
---

TL;DR: Cancel a running GitLab CI pipeline. QUICK START: gitlab_ci_cancel_pipeline(project_id='namespace/project', pipeline_id=12345) cancels pipeline. USE CASES: (1) Stop unnecessary pipeline execution, (2) Cancel failed deployment, (3) Abort long-running jobs. RETURNS: Confirmation with pipeline_id. PERMISSIONS: Requires repository:write (GitLab CI write access). EXAMPLE: gitlab_ci_cancel_pipeline(project_id='namespace/project', pipeline_id=12345)