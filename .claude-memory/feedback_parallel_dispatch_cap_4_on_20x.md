---
name: feedback-parallel-dispatch-cap-4-on-20x
description: "Dispatch cap follows the account tier: 20x = up to THREE parallel agents (workers + reviewers combined, 2026-10-10); 5x = one worker + one reviewer. On extra usage: no timer-based polling."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-10T21:52:43.567Z
---

The parallel-agent cap follows the subscription tier the owner is on:

- **20x account (2026-10-10):** up to THREE agents at once, counting workers and reviewers together. Owner: "we are in a 20x account. you can run up to three parallel agents".
- **5x account (2026-10-07):** ONE working agent plus at most ONE review agent.

**Why:** 2026-10-07 the 20x account hit its weekly limit mid-session with 4-5 agents running, killing every agent mid-edit; the owner then moved to 5x with a 1+1 cap. On 2026-10-10 the session went onto extra usage and the owner said: "don't launch any timer based event. we are on extra usage" -- every periodic monitor tick costs paid usage.

**How to apply:**
- Ask which tier is active if unsure; never exceed the stated cap, never go back to 4-5.
- Parallel workers in the one shared tree must touch DISJOINT files, and every brief forbids git checkout/restore/reset/stash/clean ([[feedback-parallel-agents-shared-tree-no-broad-git-ops]]); commit each worker's files by exact path.
- Reviews: Codex first for complex or security work; a Claude (Opus) reviewer where dual review is mandatory or Codex is unavailable.
- Do cheap mechanical steps (staging, targeted tests, commits) in the main context.
- Keep briefs bounded ([[feedback-bound-every-agent-run]]); a cut-off agent leaves partial edits, so check the tree before re-dispatching.
- On extra usage: no Monitor ticks, ScheduleWakeup or periodic sampling; rely on agent completion notifications and tell agents not to use timer-based polling.
