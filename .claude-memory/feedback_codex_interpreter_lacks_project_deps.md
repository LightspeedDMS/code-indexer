---
name: feedback_codex_interpreter_lacks_project_deps
description: "The codex half of tdd-paired-engineer runs a different Python interpreter without the project's deps, so it cannot run pytest — its review degrades to diff-reading only"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-11T10:03:12.491Z
---

In `tdd-paired-engineer` runs on this project, the **codex** agent runs a different Python
interpreter (3.11) from the project's environment (3.9) and lacks the project's dependencies.
Observed failures: `ModuleNotFoundError: No module named 'jose'`, and missing `fastapi`.

**Consequence:** codex cannot execute the test suite at all. On two separate runs in one
session (Story #1593 and its follow-up fix run) codex's review value degraded to **diff-reading
only** — every executed test number came from claude or from the orchestrator. In the first of
those runs codex reported RED/GREEN claims for REST and protocol tests it had **never actually
executed**; it disclosed this honestly rather than fabricating, which is the good outcome, but
it means those claims carried no independent verification.

**Why this matters:** the entire justification for paying the pair's higher cost is that two
differently-trained models with different blind spots verify each other. If one of them cannot
run the code, half the verification is gone and what remains is one model reviewing diffs.
Pairing still caught real defects this way — codex found an ordering defect in an agreement, and
on another run a route-shadowing bug was caught by *claude* reviewing codex's files — so the
value is not zero. But do not assume both halves are executing tests.

**How to apply:**
- When dispatching `tdd-paired-engineer` on this project, expect that only one side can run
  pytest. Ask explicitly in the brief for each agent to state WHICH side executed each quoted
  test result, so an unexecuted claim is visible rather than implied.
- Treat any RED/GREEN number in a pair report as unverified until the orchestrator re-runs it.
  This session's re-runs caught a discrepancy the pair missed: a reported "0 failed" that was
  actually 1 failed on an independent run.
- Fixing the interpreter mismatch (giving codex the project venv) would restore genuine
  two-sided verification and is worth doing before relying on the pair for test evidence.

See [[feedback_paired_engineer_two_models_for_quality]] for why the pair is used at all, and
[[feedback_verify_codex_actually_ran]] for the related failure where codex-wrapper agents fall
back silently.
