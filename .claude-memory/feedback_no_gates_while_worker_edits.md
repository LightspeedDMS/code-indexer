---
name: feedback-no-gates-while-worker-edits
description: "Never run a full test gate (fast/server-fast/e2e) while an agent is editing the shared working tree: the gate imports live src and records the agent's half-written code as failures"
metadata:
  type: feedback
---

Run full gates only when no agent is editing source in the same working tree. The gates import `src/` live (and e2e spawns `cidx` subprocesses from it), so an agent's half-finished edit becomes a gate failure that says nothing about the committed code.

**Why:** 2026-10-08 an e2e run went red in Phase 3 because a worker was mid-edit in `cli_daemon_delegation.py` (a helper used `List` before its import was added); every `cidx init` subprocess crashed with `NameError`. The run had to be repeated in full.

**How to apply:**
- Schedule gates in the gaps between workers, or have the worker run only targeted tests while a gate is running and forbid it from editing until the gate ends.
- Read-only reviewers (Codex relays) can run alongside a gate; editing workers cannot.
- A gate run concurrent with edits is not evidence; repeat it on a quiet tree. Related: [[feedback-parallel-dispatch-cap-4-on-20x]].
