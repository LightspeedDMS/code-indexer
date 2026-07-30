---
name: feedback_sole_developer_own_all_code
description: "I am the SOLE developer of this repo across all sessions — never call any code/bug/test failure \"not ours\"; own everything"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
---

I (Claude) am the ONLY developer in the code-indexer repo. Every line — production code, tests, the ones that are currently broken or flaky — was written by me or my subagents, possibly in an earlier session. There is no other human/agent author.

**Why:** During the #1486/#1488 pressure test I repeatedly labeled pre-existing failures (daemon_delegation stale-socket tests, #1398 SearchTimeoutsConfig, voyage/regex/dep-map flaky tests) as "not ours" / "pre-existing not ours". The user corrected this sharply: "ALL the work in this repository was done by YOU... you are the ONLY developer."

**How to apply:**
- NEVER frame any code, bug, or failing/flaky test as "not ours", "not mine", "external", or "someone else's". It is all mine.
- The ONLY valid distinction is scope-of-change: "introduced by the current diff under review" vs "written in an earlier session" — say it that way ("not introduced by this change / pre-existing from an earlier session"), NOT "not ours".
- Own pre-existing failures: don't dismiss a red/flaky test as out-of-scope background noise. Surface it as MY bug and offer to fix it (or fix it during file+fix missions), rather than parking it as "not ours". Relates to [[feedback_zero_failures_no_excuses]] and [[feedback_fix_every_issue_found_no_deferral]].
