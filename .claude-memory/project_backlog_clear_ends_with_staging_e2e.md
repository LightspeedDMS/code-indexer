---
name: project-backlog-clear-ends-with-staging-e2e
description: "User wants the backlog-clear saga to end with staging E2E testing once the bug backlog is down to priority-4 (sev4) only -- open for discussion at that point, not before"
metadata:
  type: project
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-28T02:03:25.803Z
---

User's explicit instruction (2026-08-27, mid-turn during the [[project_backlog_clear_to_zero_mandate]] sweep): "remember to end the entire saga with staging testing once we have cleared the backlog. I'm open to discuss doing this when we have sev 4 only open."

**How to apply**: once the open bug-labeled issue count converges to priority-4-only (or zero), raise staging E2E testing as the closing step of this whole backlog-clearing effort -- do not launch it earlier, and do not launch it unilaterally even at that point; the user wants to discuss it first. This is the natural conclusion criterion for the standing [[project_backlog_clear_to_zero_mandate]] convergence definition.

**Context**: this session has been running a continuous 4-agent-parallel bug-fix sweep (tdd-engineer -> code-reviewer -> push -> close) against the code-indexer GitHub bug backlog. Fixes land on `development` then get merged to `staging` (auto-deploys) as part of the normal per-batch workflow, but that is routine deployment, not the "staging testing" the user means here -- this note is about a dedicated E2E validation pass (per this project's `e2e-automation.sh` / server-fast-automation.sh discipline, or a manual staging exercise) run specifically as the wrap-up once the bug count has converged.
