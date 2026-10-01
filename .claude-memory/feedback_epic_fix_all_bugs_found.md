---
name: feedback_epic_fix_all_bugs_found
description: "On a large epic/effort, fix ALL bugs found while snorkeling through the code — including pre-existing/unrelated ones — not just the in-scope ones"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
---

When working a large epic or multi-round effort, fix EVERY bug encountered while moving through the code — including pre-existing failures, flaky tests, and issues unrelated to the immediate task. Do not "document as pre-existing / out of scope" and move on. The bar is a genuinely clean, green suite.

**Why:** During the #1486/#1488 pressure test I kept parking pre-existing red/flaky tests (daemon_delegation stale-socket + retired-API assertions, #1398 SearchTimeoutsConfig, voyage/regex/dep-map load-flaky tests) as "pre-existing, not blocking this diff." The user: "when you are working on a large epic... you fix ALL the bugs found as you snorkel thru the code. simple." Combined with [[feedback_sole_developer_own_all_code]] (every line is mine), there is no such thing as an out-of-scope bug during a large effort.

**How to apply:**
- During any epic/large effort: when a red or flaky test surfaces (even in an unrelated subsystem), FIX it in the same effort — don't defer, don't label it someone else's.
- Stabilize flaky-under-parallel-load tests too (isolation-passing ≠ acceptable) — a flaky test is a bug.
- Extends [[feedback_fix_every_issue_found_no_deferral]] and [[feedback_zero_failures_no_excuses]]: those cover in-scope defects; this makes it explicit that the "snorkel radius" of a large effort includes everything you swim past.
