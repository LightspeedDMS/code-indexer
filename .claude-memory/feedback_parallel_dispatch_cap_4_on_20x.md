---
name: feedback-parallel-dispatch-cap-4-on-20x
description: "Dispatch cap is now ONE working agent + at most ONE review agent (5x subscription, 2026-10-07); Codex carries the hardest reviews. Supersedes the earlier 4-agent cap on 20x."
metadata:
  type: feedback
---

On the 5x subscription the coordinator runs at most ONE working agent (engineer, assembler, investigator) and at most ONE review agent at a time. The hardest reviews go to Codex (codex-code-reviewer relays), and Claude reviewers are used sparingly.

**Why:** 2026-10-07 the 20x account hit its weekly limit mid-session with 4-5 agents running, killing every agent mid-edit. The owner switched to a 5x account: "can't go with 4 agents at a time ... one agent doing work, and max one agent doing reviews, and rely on codex for the most complicated reviews. this is a 5x account". (Earlier, on 20x, the cap was 4.)

**How to apply:**
- Keep a single ordered work queue; dispatch the next work item only when the current worker reports.
- Reviews: Codex first for complex or security work; a Claude (Opus) reviewer only where dual review is mandatory and Codex has already passed it, or when Codex is unavailable.
- Do cheap mechanical steps (staging via hash-object/update-index, exporting, running targeted tests, committing) in the main context instead of spending an agent.
- Keep briefs tight and budgeted ([[feedback-bound-every-agent-run]]); a cut-off agent leaves partial edits, so check imports and the tree before re-dispatching.
- If the owner moves back to a larger tier, ask before raising the cap.
