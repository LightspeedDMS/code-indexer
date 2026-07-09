---
name: feedback_study_anomalies_deeply
description: "When you see odd/anomalous behavior, study it in depth to root cause — NEVER dismiss it as an artifact without proof"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9f2bc45f-0085-4f49-90f3-7e65bdd67bcf
---

When something odd surfaces during testing/investigation (an asymmetric result, an unexpected value, a weird shape/number, a surprising log line), STUDY IT IN DEPTH until the root cause is proven. Do NOT dismiss it as a "single-query artifact", "fixture issue", "benign", "cosmetic", or "probably fine" without hard evidence that actually proves that classification. A dismissal is a claim and requires proof just like a bug does.

**Why:** During the epic #1289 clean-code E2E, voyage-context-4 temporal recall returned uniformly-low scores and missed the ground-truth commit while cohere nailed it rank-1, and the voyage shard had an odd (1024->64) projection_matrix. The instinct to file it as "maybe a weak query, continue" is exactly the wrong move — the user explicitly wants odd things studied to root cause, never dismissed. Anomalies are where the real bugs hide (the whole epic's value came from real front-door testing catching the voyage_ai.py HTTP-400 server-path bug that all unit tests missed).

**How to apply:** On any anomaly: (1) reproduce it deterministically; (2) instrument the exact code path (call stacks, intermediate values, shapes/dims) rather than theorizing; (3) compare the working vs broken case side by side (e.g. cohere-works vs voyage-broken); (4) run MULTIPLE variations to distinguish a real defect from a true one-off; (5) only classify it (bug vs benign) once the evidence proves the classification. If it IS a bug, file it and fix it. If it's genuinely benign, prove WHY with evidence. "I didn't have time" / "probably fine" is never an acceptable resolution. Relates to [[feedback_prove_root_cause_before_fix]], [[feedback_zero_failures_no_excuses]], [[feedback_never_stop_never_blame_env]].
