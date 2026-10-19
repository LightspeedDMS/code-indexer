---
name: feedback_dont_reask_deferred_promotion
description: "When the owner defers a production promotion (\"we will push X later\"), never mention or ask about it again until they bring it up"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-19T02:21:39.558Z
---

When the owner says a validated release will be promoted "later", stop listing it as an open
decision or reminder in status updates. They will raise it themselves.

**Why:** 2026-10-18 the owner deferred 12.91.0's promotion; it was then repeated as an "open
decision" in several status messages, and the owner said "don't ask again about 12.91 promp".

**How to apply:** record the deferral once (tracker/plan), then omit it from every later summary.
The push-to-master protocol still applies in full when the owner does raise it. Related:
[[feedback_no_unnecessary_questions]].
