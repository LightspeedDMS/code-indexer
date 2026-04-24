---
name: gitlab_ci_retry_pipeline
category: cicd
required_permission: repository:write
tl_dr: Retry a failed GitLab CI pipeline.
---

TL;DR: Retry a failed GitLab CI pipeline. QUICK START: gitlab_ci_retry_pipeline(project_id='namespace/project', pipeline_id=12345) triggers retry. USE CASES: (1) Retry flaky test failures, (2) Re-run after fixing issue, (3) Resume failed deployment. RETURNS: Confirmation with pipeline_id. PERMISSIONS: Requires repository:write (GitLab CI write access). EXAMPLE: gitlab_ci_retry_pipeline(project_id='namespace/project', pipeline_id=12345)