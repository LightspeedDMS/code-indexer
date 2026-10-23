---
name: project-package-12-94-resume-state
description: "READ FIRST after a reset during the 12.94 package - reviewed, all gates green, bump committed; waiting for the owner's push"
metadata:
  node_type: memory
  type: project
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-23T15:09:05.911Z
---

Package after v12.93.0 on development (2026-10-22/23). Scope: change detection for local catalog
sources and the catalog refresh debouncer, catalog writer coordination, the golden-repo write lock
lease and write-mode ownership, reader leases, Groups bulk repository access, the Rust lockfile
build, test-suite hygiene, plus fixes found by the gates (reconcile exit codes, search-event flush).

State 2026-10-23 ~10:10: every lane done, dual-reviewed (Codex + Claude) and manually tested
through the front doors. All gates green on the final code: lint; server-fast 29,808 passed;
fast 20,156 passed (its only failures were stale tests since fixed and re-run, plus one load flake
re-run green); e2e phases 1-7 all passed (phases 4-7 run individually after intermittent
environment trips: a test-harness throttle gap and an external provider latency warning).
CHANGELOG committed; MINOR bump to 12.94.0 committed as the push tip. NOTHING PUSHED.

12.93.0 was promoted to production on 2026-10-22 with the owner's phrase and both confirmations;
the owner confirmed the production health check.

**How to apply (remaining steps):**
- Wait for the owner. On the owner's word: push origin development (the bump must stay the tip);
  also back-merge public/master into origin/development (content-free); merge development into
  staging and push; validate solo + cluster staging through the front doors; STOP again.
- Never push master without the phrase + two confirmations.
- If anything else is committed before the push, the bump must be redone as the new tip.
- Follow-ups to file live in the gitignored reports/package_12_94_followups.md.

Related: [[feedback-commit-issue-refs-overlap]], [[feedback-dispatch-cap-by-tier]],
[[feedback-version-bump-must-be-push-tip]].
