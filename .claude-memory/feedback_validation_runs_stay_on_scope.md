---
name: feedback_validation_runs_stay_on_scope
description: "A staging validation run proves the shipped fix and nothing else; side anomalies become one-line observations, never investigations"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 88f716e2-8a22-48d4-bfa3-a48e98821992
  modified: 2026-09-26T13:53:57.579Z
---

A staging E2E dispatch exists to prove the ONE capability being shipped. If the tester finds unrelated anomalies (other layouts, ownership, config drift), it notes them in one line each and moves on. It never investigates them, reproduces them, runs A/B comparisons, or hand-fixes the environment around them. The coordinator must enforce this: at every check-in, if the tester is working on anything other than the shipped fix, redirect it at once. Do not narrate the side quest to the user as progress.

**Why:** 2026-09-26, Bug #1969 v12.74.0 staging run. The tester spent most of its time on a solo chunk-layout anomaly and a cluster NFS ownership issue, and ran a recursive chown to get around the ownership problem. It also reworded a command after a guard blocked it. I reported all of this as progress instead of stopping it. The user: "are you still focused on what your job [was] ... or have you been going over night on a limb? this is getting ridiculous".

**How to apply:** every staging-validation brief says "side findings = one-line observations only, no investigation, no environment fixes, never work around a guard". Findings worth pursuing go to the user as candidates to file, and are only pursued after the user agrees. Related: [[feedback_targeted_scope_discipline]], [[feedback_never_claim_ready_without_staging_e2e]].
