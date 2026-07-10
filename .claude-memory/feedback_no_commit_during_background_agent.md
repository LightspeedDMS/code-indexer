---
name: feedback_no_commit_during_background_agent
description: "Never git-add/commit a background agent's files while it is still running — the add can snapshot a reverse-applied (broken) intermediate state"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ffe9e7f2-e6bc-4fbe-b9f5-45e1f7b8661d
---

Do NOT `git add`/commit files that a still-running background subagent (e.g. tdd-engineer) is editing. `git add` snapshots whatever is on disk at that instant, and a TDD agent's RED->GREEN dance momentarily REVERSE-APPLIES its own patch (to prove the test fails) before forward-applying it. If your commit lands during that window, you capture a broken intermediate: e.g. the new TEST file committed but the SRC fix absent, so the committed unit fails at import/collection.

**Why:** This actually happened (Bug #1256, v11.14.0). Commit `fe54f0fa` captured `test_job_tracker_update_dedup_1256.py` but NOT the `job_tracker.py`/`background_jobs.py` fix — because the engineer had momentarily reverse-applied the src patch when `git add` ran. The committed test imported a helper that wasn't in the committed src. Working-tree tests passed (tree had the fix); the COMMIT was broken. Both I and the code-reviewer independently caught it only by `git show <sha>:<file>` / `git show --name-only`, not by running the working tree.

**How to apply:**
- Wait for the background agent's REAL completion (its actual final report, not tangled relay notifications) before staging its files. Verify the deliverable on disk first.
- After committing an agent's work, VERIFY the commit content, not just the working tree: `git show HEAD:<path> | grep <expected-symbol>` for each src file, and `git show HEAD --stat` to confirm every expected file is in the commit.
- If a broken commit is already made and UNPUSHED, `git commit --amend` after `git add`-ing the missing files is the clean repair (rewrites local history only). Confirm `origin/<branch>` is still behind the broken SHA before amending.
- Agent-framework relay can send confused/duplicate "finished" notifications (meta-messages like "I'll wait for the tdd-engineer") — treat the FILES ON DISK + your own verification as ground truth, not the notification text. Related: [[feedback_faithful_db_mocks]], [[project_test_gates_flake_under_load]].
