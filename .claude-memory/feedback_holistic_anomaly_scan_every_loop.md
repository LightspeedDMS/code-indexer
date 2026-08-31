---
name: feedback_holistic_anomaly_scan_every_loop
description: "Every loop, don't just verify the narrow change — holistically scan logs + jobs table + admin UI for patterns of oddities, like a human operator"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

Every development/verification loop, it is NOT enough to naively confirm that the specific change I made works. I MUST ALWAYS also step back and holistically review the system as a pair of human eyes on the admin UI would — hunting for patterns of oddities, not just green checkmarks on the narrow change.

**Why:** narrow "does my change work" verification misses systemic problems. In this project the two worst bugs were found ONLY by looking around, never by the targeted test: the fleet-migration NFS data-loss (found via the `background_jobs` table showing 3000+ jobs + 481 `database disk image is malformed` failures) and the RA stale-`folder_path` cluster breakage (found via server logs, not the change under test). An "odd" number, a repeated failure, or an unexpected job volume is a lead, never noise.

**How to apply (every loop, especially after any staging change):**
- **Jobs table**: query `background_jobs` for status counts per `operation_type`, recent failures + their `error`, and abnormal volumes/rates (e.g. a scheduler firing every minute). Repeated failures / quarantine loops / runaway job counts = investigate to root cause.
- **Logs**: grep server logs (journald + the log store) for ERROR/WARNING patterns, not just the one string related to my change. Look for anything recurring.
- **Health / admin UI surfaces**: `/health` status + `failure_reasons`, dashboard recent-jobs, per-node metrics. `degraded` is a lead to chase, not to note-and-move-on.
- **On-disk / data**: when relevant, sanity-check the actual artifacts (integrity_check on SQLite, file counts, layouts) — the naive API "200 OK" can hide a corrupt/half-written store.
- Any oddity gets root-caused with facts before I call a loop done. See [[feedback_study_anomalies_deeply]], [[feedback_review_local_and_staging_logs_after_testing]], [[feedback_no_half_wired_features]].
