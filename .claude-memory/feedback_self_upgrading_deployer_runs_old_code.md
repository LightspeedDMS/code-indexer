---
name: feedback_self_upgrading_deployer_runs_old_code
description: "A self-upgrading auto-updater runs the OLD in-memory code during the very deploy that installs the fix — validate the transitional deploy, not just steady state."
metadata:
  node_type: memory
  type: feedback
  originSessionId: a8e442d6-0c34-4c2b-8b4c-30d7e4ed0283
  modified: 2026-09-17T22:01:45.775Z
---

The cluster auto-updater is a per-tick root process: it imports code_indexer modules, then does
`git pull` + `pip install` of the new version, then runs `deployment_executor` steps IN THE SAME
process. Python caches imported modules, so any deploy-time code (e.g. the bootstrap-config writer
`deployment_executor.py:~5619 write_json_atomic(config_path, ...)`) runs the OLD version's code that
was imported before the pull — even though the on-disk source is now the new version.

Consequence: a fix to a deploy-time code path does NOT take effect on the deploy that installs it;
it takes effect on the NEXT deploy. This is exactly how #1896 recurred once on the 12.64.0->12.65.0
staging deploy after the fix was already shipped (the process still held 12.64.0's unfixed
write_json_atomic), then stopped.

**Why:** node availability can depend on a deploy-time write, and a bug in that write "heals itself"
only one deploy late — so the deploy that ships the fix can still break the node.

**How to apply:**
- When fixing anything the auto-updater/deployment_executor executes at deploy time, reason about
  the TRANSITIONAL deploy (old code running while new code installs), not just steady state.
- After shipping such a fix to staging, EXPECT the old-code behavior once more on that deploy; verify
  the node recovers and check the fix directly against the DEPLOYED new code (e.g. run the fixed
  function as the privileged user over a realistic target and assert the outcome) rather than trusting
  the deploy alone.
- Production transition safety depends on what the OUTGOING version's deploy code does: verify it
  (e.g. `git show origin/master:.../deployment_executor.py`). An in-place `open(path,"w")` preserves
  inode+owner; a mkstemp+os.replace flips ownership when run as root over a differently-owned file.

Related: [[project_release_1873_1876_overnight_state]], [[feedback_bootstrap_changes_need_installer_and_autoupdater]],
[[feedback_never_claim_ready_without_staging_e2e]], [[project_cluster_auto_updater_service]].
