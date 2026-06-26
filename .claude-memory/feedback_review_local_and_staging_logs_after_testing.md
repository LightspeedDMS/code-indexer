---
name: feedback_review_local_and_staging_logs_after_testing
description: "After all testing completes, ALWAYS audit BOTH local and staging logs; if a pattern points to a bug, file AND fix it"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ffe9e7f2-e6bc-4fbe-b9f5-45e1f7b8661d
---

After completing development work and testing, you MUST finish by reviewing BOTH the local server logs AND the staging logs. This is not optional and not a spot-check of only the thing you changed — audit the actual running state of both environments.

When the logs reveal a PATTERN of issues that all point to a bug, you must FILE a bug issue AND FIX it through the full workflow (tdd-engineer -> code-reviewer -> manual-test-executor). Do not merely report it back and wait — if the evidence converges on a bug, treat it as one.

**Why:** A large effort with many fixes and rapid deploys (e.g. v10.163->168 in one cycle) can introduce regressions or surface latent failures. You cannot declare success on the strength of unit/integration gates alone — green test suites do not prove the live cluster is healthy. The user was (rightly) annoyed that defects (failing langfuse global_repo_refresh, intermittent cross-node login 500, jwt_secret divergence) were found by him, not by a proactive log audit after the deploy cycle.

**How to apply:**
- After the final gate passes and changes are on staging, run a full ERROR/WARNING inventory across the shared PG log store AND every node's per-node logs (journalctl + logs.db) — per-node-only errors are invisible in the shared store.
- Time-correlate each error signature against the deploy/restart times to separate pre-existing noise from regressions your changes introduced.
- Account for ALL failed background jobs (operation_type + error), not just the ones the user noticed.
- Also check the LOCAL dev server logs after local testing.
- For any signature whose evidence converges on a real bug: open an issue and fix it, don't just narrate it.

Related: [[feedback_description_refresh_scheduler_requires_staging_validation]], [[feedback_server_e2e_front_door_only]], [[feedback_prove_root_cause_before_fix]], [[feedback_zero_failures_no_excuses]].
