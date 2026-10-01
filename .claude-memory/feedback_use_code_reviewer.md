---
name: feedback_use_code_reviewer
description: Use codex-code-reviewer as the primary reviewer; never skip the review gate because lint and unit tests are green
type: feedback
originSessionId: f59c0b85-76c2-44e4-b18c-0cc9fc093617
modified: 2026-09-07T23:38:18.969Z
---
Use `codex-code-reviewer` for code review in this project. On 2026-09-07 the user instructed:
"launch a comprehensive code review with Codex, and keep using codex for review". This SUPERSEDES
the earlier (2026-04-30) rule that said to prefer opus `code-reviewer` and avoid codex to conserve
credits. Pairing it with `code-reviewer` (opus) for a dual review is still valuable -- see
[[feedback_dual_review_claude_and_codex]].

**Why:** Codex earns its place on findings, not preference. On the Epic #1786 / #1811 X-Ray graph
work it returned REJECT with five HIGH findings on a tree that had already passed `./lint.sh`
(4 gates) and 8,017 green unit tests: no concurrency limiter at ~900-repo scale, a timeout that
did not cover the whole-repo NFS walk it claimed to cover, a cluster cache identity computed from
the wrong (legacy) evaluator assembly, a malformed facts file silently becoming an empty-but-
successful analysis, and missing JSON fields defaulting into plausible success.

**The deeper lesson (this is the part that matters):** green lint + green tests are NOT a review
and must never be used as a reason to skip one. They cannot see a dispatcher cap that silently
discards completed work, a completeness signal that reports "verified clean" for languages it
never analyzed, or a test that cannot fail for the bug it claims to guard. The user's words when
this was skipped: "are you doing full code reviews with a second agent call? ... and you must do
that". See [[feedback_tdd_red_must_be_discriminating]].

**How to apply:** Invoke `subagent_type: "codex-code-reviewer"` for review steps (dual-review with
`code-reviewer` opus where the stakes justify two passes). Applies to the mandatory workflow,
`/implement-backlog`, `/troubleshoot-and-fix`, and any skill with a review gate. Verify codex
actually ran rather than silently falling back -- see [[feedback_verify_codex_actually_ran]].
Feed findings from BOTH reviewers into ONE consolidated, deduplicated list before dispatching
fixes, and queue follow-up fixes onto the ALREADY-RUNNING fix agent via SendMessage rather than
spawning a competing agent in the same files.
