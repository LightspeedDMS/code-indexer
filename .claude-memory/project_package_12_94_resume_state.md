---
name: project-package-12-94-resume-state
description: "READ FIRST after a reset - 12.95.0 validated on staging; 12.96.0 (watermark, never-indexed paths, HNSW visibility, catalog private trees) committed on development, bump + staging push/validation next, then stop"
metadata:
  node_type: memory
  type: project
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-26T21:20:24.947Z
---

12.94.0 (package after v12.93.0): pushed to development and staging on 2026-10-23 and validated
through the front doors on both staging environments (solo and cluster). Production stays on 12.93.0;
the owner decides promotion.

12.95.0 (2026-10-24): local-source removals route the refresh to `--reconcile`; git-aware reconciles
keep HNSW visibility convergent. Bumped, pushed to development and staging, validated through the
front doors on both staging environments. Reports in gitignored reports/staging_validation_12_95_*.md.

12.96 package (owner-approved scope 2026-10-24, paused and resumed by the owner): dev issues 2195
(reconcile watermark), 2196 (non-UTF-8 names skipped), 2197 (quiet incremental republish), 2198 (lease
files never indexed), 2199 (empty-reconcile chunk layout marker), 2200 (branch-change listing failure),
plus catalog private trees (owner + service group only). Review hardened it further: HNSW loaders never
load a point hidden on the branch, hide/un-hide failures fail the run with typed exit codes, failed or
cancelled git-aware runs abort and leave the publish owed, duplicate cleanup pages and retries, watch
retries with per-path backoff, trigram index tolerates non-UTF-8 names, e2e catalog world locks through
the server's lock store. Dual review per lane (Claude + Codex; coordinator arbitrated repeated Codex
rejections, rulings in the gitignored follow-ups file). Manual front-door test on an isolated server
passed. Gates (2026-10-25): lint green; fast 20,355 passed (one stale test double fixed, one timeout
flake); server-fast 29,861 passed (one timing flake); e2e all 7 phases green (phase 4 log audit caught
a harness lock race, harness fixed, phases 3-7 re-run green). Committed on development: security change
alone (0a56a22a7), indexing/server/watch fixes (f5e6843d0), then docs (CHANGELOG, CLAUDE.md).

Update 2026-10-25: 12.96.0 shipped to staging, but its catalog permission pass treated any chmod
failure as fatal, and shared storage can hold entries the service user does not own (dev 2202). Lesson:
a permission pass must change only what the process owns and report the rest, never block a publish.
Fixed and shipped as 12.97.0 (tag v12.97.0), VALIDATED 2026-10-25 on both staging environments through
the front doors; details in gitignored reports/staging_validation_12_97_*.md. STOPPED at staging,
waiting for the owner.

NEXT (2026-10-25, owner-approved): 12.98.0 on development; full gates green on 2026-10-27 (lint, rust,
fast, server-fast, e2e all 7 phases), then bump and staging validation of both environments. Its scope, order and owner decisions are tracked outside this repository, never in
this file. Changes to the pace-maker hook project are ours to make, deploy to staging and test, and
the code-indexer auto-updater must deliver them.

AFTER 12.98 reaches staging (owner, 2026-10-26): the test-suite package. Move every test that starts
a real `cidx` process out of fast-automation and server-fast into slow-automation, add a guard that
fails the fast suites on an unmarked real-process test, make slow-automation a required gate, fix the
tokenizer load lock (dev 2204), rebalance server-fast chunks. Analysis and plan in gitignored
reports/fast_suites_slowness_analysis_2026-10-26.md and reports/move_real_cidx_tests_plan_2026-10-26.md.
See [[feedback-fast-automation-unit-tests-only]].

**How to apply (remaining steps):**
- For each package: TDD, Claude + Codex review, full gates once, CHANGELOG + MINOR bump as the push
  tip, push development, merge to staging, validate solo AND cluster through the front doors, then
  WAIT for the owner. Never offer or ask about production promotion.
- Any new development commit needs a new MINOR bump as the push tip before the next staging push.
- Follow-ups (including dev 2201, watch re-embed on a failing store) are in the gitignored
  reports/package_12_94_followups.md.

Related: [[feedback-commit-issue-refs-overlap]], [[feedback-version-bump-must-be-push-tip]],
[[feedback_dont_reask_deferred_promotion]].
