---
name: gitlab_ci_retry_pipeline
category: cicd
required_permission: repository:write
tl_dr: Retry a failed GitLab CI pipeline.
inputSchema:
  type: object
  properties:
    project_id:
      type: string
      description: Project in 'namespace/project' format or numeric ID
    pipeline_id:
      type: integer
      description: Pipeline ID to retry
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

TL;DR: Retry a failed GitLab CI pipeline. QUICK START: gitlab_ci_retry_pipeline(project_id='namespace/project', pipeline_id=12345) triggers retry. USE CASES: (1) Retry flaky test failures, (2) Re-run after fixing issue, (3) Resume failed deployment. RETURNS: Confirmation with pipeline_id. PERMISSIONS: Requires repository:write (GitLab CI write access). EXAMPLE: gitlab_ci_retry_pipeline(project_id='namespace/project', pipeline_id=12345)