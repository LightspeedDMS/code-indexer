---
name: gitlab_ci_cancel_pipeline
category: cicd
required_permission: repository:write
tl_dr: Cancel a running GitLab CI pipeline.
---

TL;DR: Cancel a running GitLab CI pipeline. QUICK START: gitlab_ci_cancel_pipeline(project_id='namespace/project', pipeline_id=12345) cancels pipeline. USE CASES: (1) Stop unnecessary pipeline execution, (2) Cancel failed deployment, (3) Abort long-running jobs. RETURNS: Confirmation with pipeline_id. PERMISSIONS: Requires repository:write (GitLab CI write access). EXAMPLE: gitlab_ci_cancel_pipeline(project_id='namespace/project', pipeline_id=12345)