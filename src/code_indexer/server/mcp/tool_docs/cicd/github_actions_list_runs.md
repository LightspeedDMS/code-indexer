---
name: github_actions_list_runs
category: cicd
required_permission: repository:read
tl_dr: List GitHub Actions workflow runs with filtering by workflow, status, or branch.
inputSchema:
  type: object
  properties:
    owner:
      type: string
      description: Repository owner (user or organization)
    repo:
      type: string
      description: Repository name
    workflow_id:
      type: string
      description: Optional workflow ID or filename (e.g., 'ci.yml')
    status:
      type: string
      enum:
      - queued
      - in_progress
      - completed
      description: Optional status filter
    branch:
      type: string
      description: Optional branch name filter
    limit:
      type: integer
      default: 20
      description: 'Maximum number of runs to return (default: 20)'
  required:
  - owner
  - repo
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

TL;DR: List GitHub Actions workflow runs with optional filtering by workflow, status, and branch. QUICK START: github_actions_list_runs(owner='user', repo='project') returns recent workflow runs. USE CASES: (1) Monitor CI/CD status, (2) Find failed workflow runs, (3) Check workflow run history. FILTERS: workflow_id='ci.yml' (filter by workflow), status='completed' (filter by status), branch='main' (filter by branch). RETURNS: List of workflow runs with id, name, status, conclusion, branch, created_at. PERMISSIONS: Requires repository:read. AUTHENTICATION: Uses stored GitHub token from token storage. EXAMPLE: github_actions_list_runs(owner='user', repo='project', status='failure', branch='main', limit=20)