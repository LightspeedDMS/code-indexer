---
name: gh_actions_list_runs
category: cicd
required_permission: repository:read
tl_dr: List recent GitHub Actions workflow runs with optional filtering by branch
  and status.
---

TL;DR: List recent GitHub Actions workflow runs with optional filtering by branch and status. QUICK START: gh_actions_list_runs(repository='owner/repo') returns recent runs. USE CASES: (1) Monitor CI/CD status, (2) Find failed workflows, (3) Check workflow history. FILTERS: branch='main' (filter by branch), status='failure' (filter by conclusion). RETURNS: List of workflow runs with id, name, status, conclusion, branch, created_at. PERMISSIONS: Requires repository:read. AUTHENTICATION: Uses stored GitHub token from token storage. EXAMPLE: gh_actions_list_runs(repository='owner/repo', branch='main', status='failure')