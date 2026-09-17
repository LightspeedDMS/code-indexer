---
name: feedback_review_findings_fix_p1_p2_tolerate_p3_p4
description: Code-review findings gate shipping only at P1-P2; P3-P4 findings are tolerated and filed as follow-up issues instead of triggering another fix round
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-17T00:32:29.178Z
---

When a code review (Claude or Codex) returns findings on a fix, only **P1-P2** findings block
shipping and must be fixed. **P3-P4** findings are tolerated: file them as follow-up issues
and keep moving toward gates and staging.

Severity rubric used to classify each finding:
- **P1** — regression versus current production behaviour; wrong results returned silently on
  common/realistic input; data loss; security; breaks a gate or test suite.
- **P2** — incorrect results on realistic input; significant hot-path performance regression;
  validation that fails OPEN on plausible input.
- **P3** — incorrect behaviour only on uncommon/exotic input; inaccurate docs or comments; test
  gaps that do not hide a real defect.
- **P4** — style, duplication, naming, cosmetic nits.

Ask reviewers to assign a severity from this rubric to every finding, so the gate decision is
mechanical rather than argued each round.

**Why:** on 2026-09-16 the X-Ray fixes (#1873/#1875 graph binder, #1876 glob scoping) went
through repeated reject rounds where each round found a smaller edge case than the last — from a
real live->dead regression, to `extends Outer.Base<String>`, to glob syntax like `{a}` and
`foo}`. Each finding was individually legitimate, but nothing shipped for many hours while the
core fixes (12 false-dead findings removed; include/exclude filters silently ignored on every
indexed repo) were already proven and strictly better than production. The user set this rule
when asked whether the loop was converging.

**How to apply:** after each review, classify every finding with the rubric. Dispatch fixes for
P1-P2 only. File P3-P4 as issues (grouping related ones). A regression versus HEAD/production is
always at least P1 regardless of how exotic the input looks. This narrows, for review findings
specifically, the broader habit in [[feedback_fix_every_issue_found_no_deferral]] — P3-P4 review
findings are filed, not fixed in-session. [[feedback_zero_failures_no_excuses]] still applies to
test gates: a failing test is never a tolerated P3.
