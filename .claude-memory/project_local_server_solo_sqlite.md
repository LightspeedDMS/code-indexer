---
name: project_local_server_solo_sqlite
description: "The local dev cidx-server (:8000) is solo/SQLite, not clustered — local E2E validates only the solo/SQLite code paths; PG/cluster paths need staging"
metadata: 
  node_type: memory
  type: project
  originSessionId: 9f2bc45f-0085-4f49-90f3-7e65bdd67bcf
---

The local dev cidx-server on :8000 runs in **solo mode** — `storage_mode: sqlite`, no `postgres_dsn` (verified in `~/.cidx-server/config.json`; a `cluster` block may exist carrying only a `node_id`, but `storage_mode=sqlite` is what governs). Therefore ALL DB operations use SQLite: golden-repo registry, `JobTracker`, `PayloadCache`, and the temporal metadata store.

**Testing-scope consequence:** local E2E (register/index/query, refresh, activation) validates ONLY the solo/SQLite branches. The PostgreSQL/cluster-specific paths are NOT exercised locally and must be validated on staging:
- The #1313 PG-backed temporal metadata backend and its cross-process env contract `CIDX_TEMPORAL_PG_BOOTSTRAP_DIR` (locally the temporal subprocess runs the SQLite branch, i.e. `env=None`).
- Cluster-aware state (`app.state.payload_cache` PG backend, BGM `JobTracker` on PG, `SharedJobSentinel` on NFS).
- PG advisory-lock migration safety, and any `storage_mode: postgres` code.

When confirming a fix locally that has both a solo and a cluster branch, the local run proves the SQLite branch; the PG branch rides on mocked unit tests until staging. Decisive local tell = SQLite `.db` files under `~/.cidx-server/`; decisive cluster tell (on staging) = rows in the shared PG DB. Complements [[project_cluster_temporal_metadata_pg_backed]] (which validates the PG side).
