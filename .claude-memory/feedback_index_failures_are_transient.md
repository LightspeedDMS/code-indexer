---
name: feedback-index-failures-are-transient
description: "Indexing \"file failures\" are transient embedder-call failures; retry them, never design caps or loop-guards for permanently failing files"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-09-28T02:56:50.691Z
---

Owner ruling (2026-09-27): a file itself "can't fail" during indexing. What fails is an embedder
call for some chunks, which is transient. Recording failed paths and retrying them on the next
run is the correct behaviour. Don't add retry caps, "only-retries exits 0" rules or other
guards against a permanently failing file. That concern is nitpicking.

**Why:** it came up during the batch D resume/retry fix. Treating hypothetical permanent
failures as a blocker would have added complexity with no real failure mode behind it.

**How to apply:** when a reviewer (Codex or Claude) raises "a permanently failing file loops
forever" against index-failure retry logic, don't treat it as blocking. Real blockers remain:
failures reported as zero, failed files never retried, or a silent partial index (see
[[feedback_opus_arbiter_of_codex_nitpicking]]).
