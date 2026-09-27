---
name: feedback-default-agents-to-sonnet5
description: "Agent model policy: implementation, TDD and general agents run on Opus 5.5; code reviews go to Codex (supersedes the earlier Sonnet-for-implementation rule)"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-09-27T13:00:00.000Z
---

CURRENT RULE (owner, 2026-09-27): run every implementation, TDD and general-purpose subagent on
Opus 5.5 (pass `model: "opus"`), and send code reviews to Codex (`codex-code-reviewer`) instead
of the Claude `code-reviewer`. The orchestrating session itself also runs on Opus 5.5.

**Why:** the owner changed the model strategy; Opus for the work, Codex as the independent
second model for review.

**How to apply:**
- Dispatch `tdd-engineer`, `general-purpose` and similar agents with `model: "opus"`.
- Use `codex-code-reviewer` for review gates. Verify Codex actually ran, because wrappers can
  silently fall back (see [[feedback_verify_codex_actually_ran]]).
- Re-run any test numbers Codex quotes, since its interpreter lacks the project deps (see
  [[feedback_codex_interpreter_lacks_project_deps]]).
- Opus 5.5 subagents tend to write their `INTENT:` declaration only inside thinking blocks, so the
  intent validator blocks every Write/Edit with "NO visible text". Proof: grouping the transcript
  records by message id showed only thinking blocks plus the tool_use. When intent validation is
  on, the Opus brief should say: "before each Write/Edit, state the INTENT: line in your reply
  text". Do NOT phrase it as "move your reasoning into visible text". That wording coincided with
  an API safeguard refusal (`reasoning_extraction`) that terminated a subagent.
- A running agent keeps its model when resumed. To switch models, stop it and relaunch it with a
  brief that says the tree already contains its partial edits.

SUPERSEDED (2026-08-17): "development/TDD on Sonnet 5, Opus only for review agents." See
[[feedback_use_code_reviewer]] for the history of the reviewer preference.
