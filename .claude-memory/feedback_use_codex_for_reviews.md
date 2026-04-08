---
name: feedback_use_codex_for_reviews
description: Use Codex (codex-code-reviewer) for all code reviews in this project, not the default code-reviewer
type: feedback
---

Always use `codex-code-reviewer` subagent (not `code-reviewer`) for code review steps in this project.

**Why:** User explicitly requested Codex for code reviews going forward (2026-04-07).

**How to apply:** In all mandatory workflow executions, replace `subagent_type: "code-reviewer"` with `subagent_type: "codex-code-reviewer"` for the review step. Also applies to `/implement-backlog`, `/troubleshoot-and-fix`, and any other skill that invokes code review.
