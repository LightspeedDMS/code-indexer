---
name: github_actions_get_run
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific GitHub Actions workflow run.
inputSchema:
  type: object
  properties:
    owner:
      type: string
      description: Repository owner
    repo:
      type: string
      description: Repository name
    run_id:
      type: integer
      description: Workflow run ID
  required:
  - owner
  - repo
  - run_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    run:
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
        commit_sha:
          type: string
        duration_seconds:
          type: integer
        created_at:
          type: string
        updated_at:
          type: string
        html_url:
          type: string
        jobs:
          type: array
        artifacts:
          type: array
---

TL;DR: Get detailed information for a specific GitHub Actions workflow run. QUICK START: github_actions_get_run(owner='user', repo='project', run_id=12345) returns detailed run info. USE CASES: (1) Investigate specific workflow run, (2) Get timing and job information, (3) View run artifacts. RETURNS: Detailed run information including jobs with steps, duration, commit SHA, artifacts, html_url. PERMISSIONS: Requires repository:read. EXAMPLE: github_actions_get_run(owner='user', repo='project', run_id=12345)