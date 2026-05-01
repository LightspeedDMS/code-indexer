---
name: feedback_use_codex_for_reviews
description: Use opus-based code-reviewer (not codex-code-reviewer) for all code reviews in this project
type: feedback
originSessionId: f59c0b85-76c2-44e4-b18c-0cc9fc093617
---
Always use `code-reviewer` subagent (model: opus, not `codex-code-reviewer`) for code review steps in this project.

**Why:** Codex credits are running low (2026-04-30). User switched back to opus-based reviewing to conserve Codex credits.

**How to apply:** In all mandatory workflow executions, use `subagent_type: "code-reviewer"` with `model: "opus"` for the review step. Do NOT use `codex-code-reviewer`. Applies to `/implement-backlog`, `/troubleshoot-and-fix`, and any other skill that invokes code review.
