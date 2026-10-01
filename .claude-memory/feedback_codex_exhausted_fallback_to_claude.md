---
name: feedback_codex_exhausted_fallback_to_claude
description: "When Codex is out of credits or its auth breaks mid-work, the coordinator explicitly switches pair work to tdd-engineer and Codex reviews to Claude code-reviewer"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 88f716e2-8a22-48d4-bfa3-a48e98821992
  modified: 2026-09-26T05:19:10.054Z
---

When Codex becomes unavailable during a task (credits exhausted, persistent 401 / auth failure), do not stall waiting for it. As coordinator, explicitly switch:
- `tdd-paired-engineer` (claude+codex) -> regular `tdd-engineer`, handing over the pair's working agreement, amendments, and current tree state.
- `codex-code-reviewer` -> Claude `code-reviewer` with the same brief.

**Why:** the user said so directly (2026-09-26, Bug #1969 round 6) after a Codex auth outage had already forced one such switch; stalling on Codex burns wall-clock for no benefit.

**How to apply:** this is a coordinator-level decision, announced to the user when it happens. The external-CLI relay agents themselves still never fall back silently (their strict-relay contract stands) -- the switch is made visibly by the main conversation, not hidden inside a relay. When Codex is healthy again, Codex stays the primary reviewer per [[feedback_use_code_reviewer]] and [[feedback_dual_review_claude_and_codex]].
