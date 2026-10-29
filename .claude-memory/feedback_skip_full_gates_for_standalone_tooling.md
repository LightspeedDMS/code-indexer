---
name: feedback-skip-full-gates-for-standalone-tooling
description: "Do not run fast-automation.sh, e2e-automation.sh (any phase) or the full lint.sh when the work is only new standalone tooling such as a performance suite; targeted tests, ruff and mypy on changed files are enough"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 7cc6211f-504b-466f-b45f-11fb9c598d63
  modified: 2026-10-28T02:41:41.913Z
---

When a change only adds standalone operator tooling (for example the performance suite from story
2214: a CLI under scripts/analysis plus its own tests and docs) and touches no product code, the owner
does not want the long gates run: not fast-automation.sh, not e2e-automation.sh (not even a single
phase), not the full lint.sh. Stated plainly: "don't run them if you only wrote a new performance test
suite".

**Why:** the gates cost 100+ minutes and exercise the product, which this kind of change cannot have
broken. I had listed them as next steps only because the project's Definition of Done says so, without
asking whether they apply.

**How to apply:** for tooling-only work, verify with the targeted tests of what was written, ruff
check/format and mypy on the changed files, and a real run of the tool itself against a real target.
Say in the final report that the full gates were skipped on the owner's instruction. If the change
also touches shared test infrastructure or product code, the gates question is open again: ask before
assuming either way. Related: [[feedback_no_unnecessary_questions]], [[feedback_rest_model_changes_need_fast_automation]].
