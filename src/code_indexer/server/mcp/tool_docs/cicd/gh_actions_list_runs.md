---
name: gh_actions_list_runs
category: cicd
required_permission: repository:read
tl_dr: List recent GitHub Actions workflow runs with optional filtering by branch and status.
inputSchema:
  type: object
  properties:
    repository:
      type: string
      description: Repository in 'owner/repo' format
    branch:
      type: string
      description: Optional branch filter
    status:
      type: string
      description: Optional status filter (e.g., 'failure', 'success')
  required:
  - repository
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    runs:
      type: array
      items:
        type: object
        properties:
          id:
            type: integer
          name:
            type: string
          status:
            type: string
          conclusion:
            type: string
          branch:
            type: string
          created_at:
            type: string
---

TL;DR: List recent GitHub Actions workflow runs with optional filtering by branch and status. QUICK START: gh_actions_list_runs(repository='owner/repo') returns recent runs. USE CASES: (1) Monitor CI/CD status, (2) Find failed workflows, (3) Check workflow history. FILTERS: branch='main' (filter by branch), status='failure' (filter by conclusion). RETURNS: List of workflow runs with id, name, status, conclusion, branch, created_at. PERMISSIONS: Requires repository:read. AUTHENTICATION: Uses stored GitHub token from token storage. EXAMPLE: gh_actions_list_runs(repository='owner/repo', branch='main', status='failure')