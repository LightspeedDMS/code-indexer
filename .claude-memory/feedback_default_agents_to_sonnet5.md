---
name: feedback-default-agents-to-sonnet5
description: "Development/TDD/implementation subagents run on Sonnet 5 (no model override); Opus is fine specifically for code-review agents"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-17T14:36:22.877Z
---

During the code-indexer bug-fixing mission (2026-08-17), I had been passing `model: "opus"`
explicitly for Claude-side review agents (`code-reviewer`). The user asked "why are you
running agents with Opus?" then said "I thought my agents should be on Sonnet 5." I
clarified I only override for review agents, never for `tdd-engineer`/implementation
dispatches (those already inherit the session model with no override). The user confirmed
the precise rule in three words: "reviews" / "opus ok" / "development, tdd, on sonnet."

**Why:** cost/architecture preference, scoped by ROLE not by stakes — review benefits from
opus's stronger adversarial reasoning (proven repeatedly this session: it caught a
catastrophic-undercount bug and a swap-vs-merge data-loss bug that mattered), but
implementation work should stay on Sonnet 5 regardless of how high-stakes the mechanism is.

**How to apply:** `model: "opus"` is approved for `code-reviewer`/`codex-code-reviewer`-type
dispatches (adversarial review, verification, judging findings). Never pass `model: "opus"`
for `tdd-engineer` or any other implementation/development dispatch — leave `model` unset
so it inherits the session's Sonnet 5. This is the STANDING rule for this user, not scoped
to one mechanism or session. See [[feedback_use_code_reviewer]] for the earlier, narrower
version of this same preference (written when Codex credits were low).
