---
name: gh_actions_cancel_run
category: cicd
required_permission: repository:write
tl_dr: Cancel a running GitHub Actions workflow.
---

TL;DR: Cancel a running GitHub Actions workflow. QUICK START: gh_actions_cancel_run(repository='owner/repo', run_id=12345) cancels workflow. USE CASES: (1) Stop unnecessary workflow execution, (2) Cancel failed deployment, (3) Abort long-running jobs. RETURNS: Confirmation with run_id. PERMISSIONS: Requires repository:write (GitHub Actions write access). EXAMPLE: gh_actions_cancel_run(repository='owner/repo', run_id=12345)