---
name: feedback-fix-every-issue-found-no-deferral
description: "Any issue discovered during active implementation/backlog work gets fixed in the same session, not just filed for later — bug filing is for organizing work, never for deferring it"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ede890d5-0358-4ba6-a741-0ed8d5e2db8f
---

During active implementation work (e.g. `/implement-backlog`), any issue discovered — including ones outside the original scope, like pre-existing test failures surfaced by a broader sweep — must be fixed before the work package is considered done. Filing a GitHub issue and moving on is not acceptable as a substitute for fixing.

User's exact words: "you find an issue, you fix it. period. no issues leaves this package of work unfixed, none, zero, nil, zilch... we file bugs for ordering ourselves, ordering our work, not to kick the can down the road."

**Why**: Filing-without-fixing lets real defects accumulate under the label "pre-existing, out of scope" even when they were legitimately found during the current work. The user wants issue tracking used as a work-queue/ordering tool (so multiple fixes can be sequenced and parallelized), not as a way to defer/avoid doing the fix.

**How to apply**:
- When a broader test sweep or investigation surfaces additional failures beyond the immediate task, still fix them in the same session — file the issue first if useful for tracking, then dispatch the fix (dispatch immediately if root cause and scope are already clear; investigate first if not).
- This does NOT mean ignoring genuine environmental/flaky-under-load noise (e.g. shared `/tmp/pytest-of-jsbattig` contention, SQLite lock contention across parallel test chunks) that is proven, via isolation re-runs, to not be a real code defect — see [[project_test_gates_flake_under_load]]. The distinction is: a test that fails deterministically (same result every isolated run) is a real issue to fix now; a test that only fails under concurrent chunk load and passes cleanly in isolation is environmental noise, not a "found issue" in this rule's sense.
- Parallelize fixes whenever they touch independent files/subsystems — the user explicitly wants this ("you paralelize when you can"), e.g. dispatching multiple tdd-engineer agents concurrently for unrelated root causes discovered in the same sweep.
- This reverses/narrows the older [[feedback_bug_report_means_report_not_fix]] memory: that memory applies ONLY when the user's literal request is "root cause + bug report" (investigate-and-stop as the explicit ask). It does NOT apply during active implementation/backlog execution, where the default is fix-everything-found.
