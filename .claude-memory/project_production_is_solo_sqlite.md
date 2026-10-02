---
name: project_production_is_solo_sqlite
description: "production runs solo/SQLite today (not clustered); SQLite is the primary backend to verify, PostgreSQL the parity check"
metadata:
  node_type: memory
  type: project
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-02T18:50:31.360Z
---

Production cidx-server runs in solo mode on SQLite (owner, 2026-10-02). It is not clustered, and production golden repos are all git-backed (including cidx-meta).

**Why:** cluster-only defects (PostgreSQL leader locks, cross-node reclaim, dead-peer detection) do not affect production today, and SQLite-specific behaviour is what users actually hit.

**How to apply:**
- Every story's tests and verification cover SQLite FIRST: query plans via `EXPLAIN QUERY PLAN`, paging, counts, routes. PostgreSQL is the parity check.
- When judging a bug's production impact, ask whether it reproduces in solo/SQLite.
- Staging solo is the production-shaped target; the staging cluster is for cluster features.

Related: [[feedback_storage_backend_dual]], [[project_local_server_solo_sqlite]], [[feedback_staging_solo_is_a_separate_host]].
