---
name: github_actions_list_runs
category: cicd
required_permission: repository:read
tl_dr: List GitHub Actions workflow runs with optional filtering by workflow, status,
  and branch.
---

TL;DR: List GitHub Actions workflow runs with optional filtering by workflow, status, and branch. QUICK START: github_actions_list_runs(owner='user', repo='project') returns recent workflow runs. USE CASES: (1) Monitor CI/CD status, (2) Find failed workflow runs, (3) Check workflow run history. FILTERS: workflow_id='ci.yml' (filter by workflow), status='completed' (filter by status), branch='main' (filter by branch). RETURNS: List of workflow runs with id, name, status, conclusion, branch, created_at. PERMISSIONS: Requires repository:read. AUTHENTICATION: Uses stored GitHub token from token storage. EXAMPLE: github_actions_list_runs(owner='user', repo='project', status='failure', branch='main', limit=20)