---
name: project-package-12-94-resume-state
description: "READ FIRST after a reset during the 12.94 package - all lanes done and reviewed; gates, CHANGELOG, bump and the owner's push remain"
metadata:
  node_type: memory
  type: project
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-23T07:15:58.662Z
---

Package after v12.93.0 on development (started 2026-10-22). Scope: change detection for local
catalog sources and the catalog refresh debouncer, catalog writer coordination, the golden-repo
write lock lease and write-mode ownership, reader leases, Groups bulk repository access, the Rust
lockfile build, test-suite hygiene.

State 2026-10-23 ~02:30: all three lanes DONE, each dual-reviewed (Codex + Claude) and manually
tested through the front doors; CLAUDE.md rules committed. Full gates running in the background
(logs and status in ~/.tmp/gates1294/). Lint failed on two stale test doubles and this memory
file not being committed; a test-only fix commit was requested and this file is being committed.

12.93.0 was promoted to production on 2026-10-22 with the owner's phrase and both confirmations;
the owner confirmed the production health check.

**How to apply (remaining steps):**
- Gates must pass: lint, server-fast, fast, e2e (re-run only what a fix touches).
- Fact-check the drafted 12.94.0 CHANGELOG entry against the commits, commit it, then commit the
  MINOR bump to 12.94.0 as the push tip.
- STOP for the owner. On the owner's word: push origin development (also back-merge public/master
  into origin/development, content-free), merge to staging, validate solo + cluster staging through
  the front doors, STOP again. Never push master without the phrase + two confirmations.
- Follow-ups to file live in the gitignored reports/package_12_94_followups.md.

Related: [[feedback-commit-issue-refs-overlap]], [[feedback-dispatch-cap-by-tier]].
