---
name: project_staging_solo_concurrent_chaos_test
description: "Another agent/session is stress/chaos-testing staging solo server concurrently with #1589/#1586/#1599/#1600 work (as of 2026-08-18)"
metadata: 
  node_type: memory
  type: project
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-19T04:39:22.224Z
---

As of 2026-08-18, a separate agent/session is running stress and chaos testing against the
staging SOLO (SQLite) cidx-server, independent of the #1589/#1586/#1599/#1600 work described in
[[project_local_server_solo_sqlite]] and this session's broader bug-fix sweep.

**Why this matters**: any anomalies observed on staging solo during this window (elevated error
rates, restarts, odd load, transient failures) may be caused by that concurrent chaos test, not by
this session's changes. Per [[feedback_no_rogue_agents]] and [[feedback_study_anomalies_deeply]],
investigate before attributing cause -- do not assume either "it's my bug" or "it's rogue/external
interference" without checking what's actually running.

**How to apply**: before running staging-solo verification for #1589/#1586/#1600 (or any other
work in this session that needs a clean staging-solo baseline), check whether the chaos test is
still active (process list, recent load, recent restarts) rather than assuming a clean baseline.
Do not restart or otherwise disrupt the staging solo server while that test may be in flight --
see [[feedback_check_running_jobs_before_restart]]. This note is time-bound to the current work
window; remove or update it once staging verification for this session's stories actually happens
and the chaos test's status is confirmed one way or the other.
