---
name: gitlab_ci_get_pipeline
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific GitLab CI pipeline.
---

TL;DR: Get detailed information for a specific GitLab CI pipeline. QUICK START: gitlab_ci_get_pipeline(project_id='namespace/project', pipeline_id=12345) returns detailed pipeline info. USE CASES: (1) Investigate specific pipeline run, (2) Get timing information, (3) View jobs and stages. RETURNS: Detailed pipeline information including jobs, duration, coverage, commit SHA. PERMISSIONS: Requires repository:read. EXAMPLE: gitlab_ci_get_pipeline(project_id='namespace/project', pipeline_id=12345)