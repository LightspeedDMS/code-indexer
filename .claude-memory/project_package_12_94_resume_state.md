---
name: project-package-12-94-resume-state
description: "READ FIRST after a reset - 12.94.0 validated on staging; 12.95.0 (local-source removal + HNSW visibility fix) committed on development, staging push/validation next"
metadata:
  node_type: memory
  type: project
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-24T08:39:27.220Z
---

12.94.0 (package after v12.93.0): pushed to development and staging on 2026-10-23 and validated
through the front doors on both staging environments (solo and cluster). Production stays on 12.93.0;
the owner decides promotion.

12.95.0 (2026-10-24): the owner approved fixing dev issue 2194 (a deleted file in a git-aware local
golden repo stayed searchable; the catalog is git-aware once its backup is configured). Fix: removals
in the local-source manifest route the refresh to `--reconcile`; git-aware reconciles keep HNSW
visibility convergent on every exit (republish after hide/unhide, single filtered publish, abort plus
durable publish-pending marker on interruption, cluster fail-closed rebuilds filtered by branch);
change-branch cleanups use `git ls-files -z`. Six review rounds (Claude + Codex) ended with both
approving. Gates: lint green; fast 20,193 passed, server-fast 29,808 passed; e2e phases 1-5 green,
6 and 7 green when run alone. Every remaining gate failure was a 15 s timeout flake under load,
re-run green alone, or the full-run phase 6 environmental stall (a slow external Claude CLI call held
the single claim slot). Manual front-door test on an isolated working-tree server passed all
scenarios. Fix and CHANGELOG committed; the version bump goes last as the push tip.

**How to apply (remaining steps):**
- Bump to 12.95.0 as the push tip, push development (CI tags), merge into staging and push.
- Validate on staging solo and cluster through the front door: create a memory, delete it, wait for
  the catalog refresh, confirm it is gone from semantic and FTS search (cluster has the backup).
- Then STOP and report. Never offer or ask about production promotion.
- Follow-ups (filed: dev 2195 watermark, dev 2196 non-UTF-8 names) and others are listed in the
  gitignored reports/package_12_94_followups.md.

Related: [[feedback-commit-issue-refs-overlap]], [[feedback-version-bump-must-be-push-tip]],
[[feedback_dont_reask_deferred_promotion]].
