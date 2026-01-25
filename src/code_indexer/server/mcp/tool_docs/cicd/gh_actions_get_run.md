---
name: gh_actions_get_run
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific GitHub Actions workflow run.
inputSchema:
  type: object
  properties:
    repository:
      type: string
      description: Repository in 'owner/repo' format
    run_id:
      type: integer
      description: Workflow run ID
  required:
  - repository
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
        created_at:
          type: string
        updated_at:
          type: string
        html_url:
          type: string
        jobs_url:
          type: string
        run_started_at:
          type: string
---

TL;DR: Get detailed information for a specific GitHub Actions workflow run. QUICK START: gh_actions_get_run(repository='owner/repo', run_id=12345) returns detailed run info. USE CASES: (1) Investigate specific workflow run, (2) Get timing information, (3) Find jobs URL. RETURNS: Detailed run information including jobs_url, updated_at, run_started_at. PERMISSIONS: Requires repository:read. EXAMPLE: gh_actions_get_run(repository='owner/repo', run_id=12345)