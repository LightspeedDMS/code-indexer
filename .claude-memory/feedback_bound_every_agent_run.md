---
name: feedback-bound-every-agent-run
description: "Every dispatched agent (especially tdd-paired-engineer) gets an explicit round/time budget in its brief and a coordinator check-in; never let one run unbounded (S15 pair ran 87 rounds / 13 h unchecked)"
metadata:
  type: feedback
---
Every agent the coordinator dispatches carries an explicit budget in the brief (paired engineer: a maximum number of pair turns, e.g. 12, and a wall-clock limit, e.g. 2 h; single engineer: a wall-clock limit), with the instruction to STOP at the budget and report status, open problems and next steps, even if unfinished. The coordinator checks progress of any agent still running past its expected duration (heartbeat file mtime, not the output transcript) and stops runaways.

**Why:** owner, 2026-10-07 ("you can't have a job like that going for 87 rounds, completely unbounded"). The S15 lease pair (tdd-paired-engineer) was launched with no round or time cap and was never checked; it ran 87 pair rounds over about 13 hours, burned the Codex credit window to zero, and left a throwaway PostgreSQL running. The pair driver's execution phase is unbounded by design, so the bound MUST come from the brief and the coordinator.

**How to apply:**
- Put `BUDGET: max N turns / H hours; at the budget, stop and report` in every pair brief, and a time budget in every single-agent brief.
- After dispatching long work, set a check-back (1200 s+ wakeup) and actually inspect `subagents/agent-<id>.jsonl` mtime and the run's progress; a run past 2x its expected time is stopped and reported to the owner.
- Large stories (like the lease protocol) are split into smaller reviewable slices instead of one open-ended mission.
- Related: [[feedback-active-monitoring-check-back]], [[feedback-no-artificial-work-budgets]] (that rule is about product code, not agent runs).
