---
name: feedback-no-scope-expansion-mid-mission
description: "Stay inside the mission: findings during a package are filed, not fixed; only at the END of the mission, and only for critical or high items, ask the owner whether to expand"
metadata:
  type: feedback
---
During a mission or package, do only what it scoped. Every finding that surfaces along the way —
review follow-ups, observations, new bugs — is FILED in the tracker, not fixed and not added to the
package. At the end of the mission, list only the critical or high ones and ASK the owner whether to
expand. Never start extra work on your own.
**Why:** owner, 2026-10-28: "you only expand asking me at the end of the mission if you found
something critical or high". The test-suite package drifted into a stream of production fixes: it
took on a pasted findings list in parallel, then fixed bugs that were only meant to be filed, and
review follow-ups pulled in more. Meanwhile the package's own deciding story (the first full lane
runs) never started.
**How to apply:**
- Before dispatching any agent, check whether its work is inside the current mission's stories. If
  not, file it instead.
- A new mission handed over mid-package queues AFTER the current package unless the owner says to
  run it in parallel.
- Review findings on the mission's own changes are in scope; P3/P4 become follow-up issues.
- Report the in-scope progress first; the out-of-scope list comes only at the end, critical or
  high only, as a question.
Related: [[feedback-targeted-scope-discipline]], [[feedback-validation-runs-stay-on-scope]].
