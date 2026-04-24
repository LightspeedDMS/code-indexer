---
name: github_actions_cancel_run
category: cicd
required_permission: repository:write
tl_dr: Cancel a running GitHub Actions workflow run.
---

TL;DR: Cancel a running GitHub Actions workflow run. QUICK START: github_actions_cancel_run(owner='user', repo='project', run_id=12345) cancels workflow run. USE CASES: (1) Stop unnecessary workflow execution, (2) Cancel failed deployment, (3) Abort long-running jobs. RETURNS: Confirmation with run_id and success status. PERMISSIONS: Requires repository:write (GitHub Actions write access). EXAMPLE: github_actions_cancel_run(owner='user', repo='project', run_id=12345)