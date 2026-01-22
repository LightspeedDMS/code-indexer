---
name: gh_actions_retry_run
category: cicd
required_permission: repository:write
tl_dr: Retry a failed GitHub Actions workflow run.
---

TL;DR: Retry a failed GitHub Actions workflow run. QUICK START: gh_actions_retry_run(repository='owner/repo', run_id=12345) triggers retry. USE CASES: (1) Retry flaky test failures, (2) Re-run after fixing issue, (3) Resume failed deployment. RETURNS: Confirmation with run_id. PERMISSIONS: Requires repository:write (GitHub Actions write access). EXAMPLE: gh_actions_retry_run(repository='owner/repo', run_id=12345)