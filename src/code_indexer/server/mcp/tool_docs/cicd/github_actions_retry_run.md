---
name: github_actions_retry_run
category: cicd
required_permission: repository:write
tl_dr: Retry a failed GitHub Actions workflow run.
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
      description: Workflow run ID to retry
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

TL;DR: Retry a failed GitHub Actions workflow run. QUICK START: github_actions_retry_run(owner='user', repo='project', run_id=12345) triggers retry. USE CASES: (1) Retry flaky test failures, (2) Re-run after fixing issue, (3) Resume failed deployment. RETURNS: Confirmation with run_id and success status. PERMISSIONS: Requires repository:write (GitHub Actions write access). EXAMPLE: github_actions_retry_run(owner='user', repo='project', run_id=12345)