---
name: feedback_paired_engineer_two_models_for_quality
description: "Prefer tdd-paired-engineer (claude+codex pairing) for gnarly/ambiguous bugs — the operator accepts its higher cost because two differently-trained models cover each other's blind spots"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-09T22:54:21.794Z
---

The operator deliberately chooses `tdd-paired-engineer` over a solo `tdd-engineer` for hard bugs,
and has said explicitly: *"it's all about quality by keeping two differently trained agents working
together. I know it has a higher cost."*

**Why:** two models trained differently have DIFFERENT failure modes, so one's blind spot is not the
other's. A single model reviewing its own work agrees with itself. The cost is the price of
disagreement, and disagreement is the product.

**Evidence from one session (2026-09-09), all three directions observed:**

- Codex caught what Claude missed: an unbounded diagnostic string (the pre-existing path capped it
  at 200 bytes, the new one didn't), and infrastructure failures being mislabelled as compile
  errors — which would have sent an agent to debug perfectly valid code.
- Claude caught what Codex missed: that a 224-line workaround was unnecessary because the CLI
  contradicted its own documented exit-code contract in two modes — and Claude DISPROVED one of
  Codex's High findings by verifying live that the bound already existed downstream.
- The pair caught what a solo agent would not: a bug report whose stated root cause was fiction
  (blamed a double-free that never existed; the real cause was toolchain resolution by cwd), AND
  that the already-shipped fix had tests structurally incapable of failing.

**How to apply:** reach for `tdd-paired-engineer` when the bug is ambiguous, the stated root cause
may be wrong, or the blast radius is memory-safety / ABI / concurrency shaped. A solo
`tdd-engineer` is fine for mechanical, well-scoped work. Do NOT argue the cost back to the
operator — the tradeoff is already decided; just note wall-time in status updates so pacing is
visible.

**Known characteristics, so expectations are calibrated:**
- Roughly 100 min and ~220k tokens for two related bugs; the first ~20 min produce NO file
  footprint because the pair is negotiating its own working agreement. That silence is normal, not
  a stall — check for a live `claude -p` process rather than concluding it is stuck.
- It negotiates methodology first (e.g. choosing sequential over parallel when two bugs touch the
  same ABI constant) and writes that agreement down.
- Fragility worth knowing: the handoff message is the ONLY channel between the two CLIs. One turn's
  hour of analysis arrived as two lines of boilerplate and survived only because it had been
  written into test doc comments.

Still verify its output yourself — it is better, not infallible. In the same session it produced a
test whose NAME overclaimed what its body proved (asserting a symbol was seeded from
`collect_facts` when that function returned an empty vec); it self-disclosed this rather than
hiding it, but the name still had to be fixed before commit. See
[[feedback_dual_review_claude_and_codex]] for the review-gate equivalent of the same principle, and
[[feedback_tdd_red_must_be_discriminating]] for what to check in its evidence.
