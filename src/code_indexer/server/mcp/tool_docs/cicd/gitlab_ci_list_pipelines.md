---
name: gitlab_ci_list_pipelines
category: cicd
required_permission: repository:read
tl_dr: List recent GitLab CI pipelines with optional filtering by ref and status.
---

TL;DR: List recent GitLab CI pipelines with optional filtering by ref and status. QUICK START: gitlab_ci_list_pipelines(project_id='namespace/project') returns recent pipelines. USE CASES: (1) Monitor CI/CD status, (2) Find failed pipelines, (3) Check pipeline history. FILTERS: ref='main' (filter by branch/tag), status='failed' (filter by status). RETURNS: List of pipelines with id, status, ref, created_at, web_url. PERMISSIONS: Requires repository:read. AUTHENTICATION: Uses stored GitLab token from token storage. SELF-HOSTED: Supports custom GitLab instances via base_url parameter. EXAMPLE: gitlab_ci_list_pipelines(project_id='namespace/project', ref='main', status='failed')