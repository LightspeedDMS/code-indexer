---
name: reference_gemini_and_agy_relays_dead_on_this_host
description: Both the gemini-* and agy-* external CLI relays fail hard on this dev machine (account tier / unsupported CPU instruction) — do not dispatch them; use codex or software-architect instead
metadata:
  node_type: memory
  type: reference
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-13T19:27:22.760Z
---

Verified 2026-09-13. Two of the four external-CLI relay families cannot run on this development
machine at all. Both failures are DETERMINISTIC, not transient — retrying, or changing
`REQUESTED_MODEL:`, will not help.

**`gemini-*` agents** (gemini-agent, gemini-code-reviewer, elite-gemini-architect,
spec-compliance-gemini-auditor, epic-spec-gemini-validator, tdd-gemini-engineer)

Fails with `IneligibleTierError` / `UNSUPPORTED_CLIENT`: the installed Gemini CLI is rejected
server-side for the individual free tier. The rejection happens during user setup, BEFORE model
selection, so no requested-model value changes the outcome. Google's remediation text points at
the Antigravity suite — which is also dead here, see below.

**`agy-*` agents** (agy-agent, agy-code-reviewer, elite-agy-architect,
spec-compliance-agy-auditor, epic-spec-agy-validator, tdd-agy-engineer)

Fails with `Illegal instruction (core dumped)`, exit 132. The Antigravity binary is compiled
requiring an x86 carry-less-multiply instruction that this machine's processor does not expose;
the Go runtime's sigill guard aborts at process start, before argument parsing, so even a
`--version` invocation core-dumps. Reproduced three times.

**How to apply:**
- Do NOT dispatch any `gemini-*` or `agy-*` agent here. Each costs roughly a minute and a
  subagent slot to return the same failure.
- Working relays: `codex-*` (verified repeatedly) and the local `opencode-*` (untested for
  quality in this project).
- When an INDEPENDENT architect or reviewer is needed and codex authored the artifact under
  review, prefer `software-architect` with `model: opus` — a Claude agent, so independent of
  codex and not dependent on either broken CLI. Self-review by the authoring agent is the
  weakest option and should be the last resort.
- Both relays FAILED CORRECTLY: each refused to fall back to another model or self-substitute,
  and reported a diagnostic instead. Trust those reports rather than re-testing.

Remedying either would mean reinstalling the Antigravity binary built for a baseline x86-64
target, or moving to an eligible paid Gemini tier. Neither is in this project's scope.

Related: [[feedback_verify_codex_actually_ran]] (codex-wrapper agents can fall back silently —
these two did not), [[feedback_dual_review_claude_and_codex]] (the standing dual-review rule that
makes an independent second opinion necessary).
