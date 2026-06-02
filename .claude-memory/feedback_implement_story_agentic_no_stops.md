---
name: feedback-implement-story-agentic-no-stops
description: "When running /implement-story-spec or any story-implementation flow, execute the entire workflow non-stop without asking pre-flight questions about scope, model choice, slicing, or process. The story breakdown is already done."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0651454f-6161-44dd-b920-259cbf9386eb
---

When the user invokes `/implement-story-spec` (or any equivalent implementation
flow on an already-designed story), execute the **entire** workflow non-stop:
TDD → review loops → manual E2E → docs → close issue. Do not pause to ask
pre-flight scope/model/slicing/authorization questions. The story has already
been broken down through the design flow — that IS the agreement.

**Why:** User explicitly stated (with emphasis): "the process is agentic, we
already broke it down so it's a story, you do EVERYTHING. non stop."
He has restated this multiple times. Each pre-flight pause is rework on a
decision already made.

**How to apply:**
- After `/implement-story-spec`: spawn tdd-engineer immediately with the full
  story content; do NOT ask whether to slice it, which model to use, or
  whether to proceed.
- Use the catalog defaults unless the user opted in via `--opus`/`--sonnet`
  flags in the slash command itself.
- Staging cluster access is pre-authorized when the story design called for
  staging E2E — do not re-confirm.
- Uncommitted unrelated files: leave them alone, instruct the agent to leave
  them alone, do not ask whether to clean them up.
- Long-running E2E (heavy dep-map analysis etc.): the manual-test-executor
  decides whether to wait or cancel after AC is proven — do not ask the user.
- The only valid reasons to stop mid-flow: (a) a hard technical blocker that
  cannot be resolved by any subagent, (b) a destructive action like
  push-to-master that requires explicit per-session authorization per
  CLAUDE.md.
- "What would you like, (a) (b) or (c)?" prompts during implementation = rule
  violation.

**Scope:** All story-implementation slash commands (`/implement-story-spec`,
`/implement-epic-spec`, `/implement-backlog`). Does NOT apply to the *design*
flow (`/design-story-spec`, `/write-epic-spec`, etc.) where step-by-step
interactivity is the whole point.

Related: [[feedback_no_unnecessary_questions]],
[[feedback_no_confirmation_on_commands]].
