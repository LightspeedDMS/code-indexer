---
name: github_actions_cancel_run
category: cicd
required_permission: repository:write
tl_dr: Cancel a running GitHub Actions workflow run.
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
      description: Workflow run ID to cancel
  required:
  - owner
  - repo
  - run_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    run_id:
      type: integer
---

TL;DR: Cancel a running GitHub Actions workflow run. QUICK START: github_actions_cancel_run(owner='user', repo='project', run_id=12345) cancels workflow run. USE CASES: (1) Stop unnecessary workflow execution, (2) Cancel failed deployment, (3) Abort long-running jobs. RETURNS: Confirmation with run_id and success status. PERMISSIONS: Requires repository:write (GitHub Actions write access). EXAMPLE: github_actions_cancel_run(owner='user', repo='project', run_id=12345)