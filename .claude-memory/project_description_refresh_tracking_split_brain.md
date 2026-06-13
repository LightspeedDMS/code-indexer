---
name: project-description-refresh-tracking-split-brain
description: "FIXED in v10.125.0 (#1100) — description-refresh scheduler now uses the registry tracking backend (PG in cluster mode), no longer node-local SQLite"
metadata: 
  node_type: memory
  type: project
  originSessionId: 873f73e7-fdb1-404d-b9f0-652abca0632f
---

RESOLVED in v10.125.0 (Bug #1100, commit 250dab4). Previously the description-refresh scheduler fell back to node-local SQLite (`~/.cidx-server/data/cidx_server.db`) even in postgres cluster mode, while `meta_description_hook` wrote PG — a split-brain that froze the PG `description_refresh_tracking` table (stuck at 2026-05-18 on staging) and starved hook-seeded repos of refreshes. Fix: `lifespan.py` resolves the registry-selected `tracking_backend` before constructing `DescriptionRefreshScheduler` and passes it in, so scheduler and hook share one backend.

**How to apply now:** In cluster (postgres) mode the LIVE description-refresh tracking store is PostgreSQL `description_refresh_tracking` (via the registry backend), NOT SQLite. Validate scheduler state against PG. To force a refresh for one repo: `UPDATE description_refresh_tracking SET next_run=<now-iso>, last_known_commit=NULL, status='completed' WHERE repo_alias=?` in PG; the scheduler loop picks it up within 60s. SQLite fallback applies ONLY to the no-registry/solo path.

**Watch-outs (proven on a live cluster, still true):** journalctl timestamps render in the server's LOCAL timezone, not UTC — convert before correlating with DB/UTC values. The scheduler runs on the node whose cluster role is `scheduler`. cidx-meta is a git-backed shared store: a forced description refresh writes + commits `cidx-meta/{alias}.md`, but a subsequent cidx-meta-global refresh/sync (Story #926 backup contract) may pull shared-remote state and remove a locally-written `.md` from HEAD — recover the refreshed content from the cidx-meta git history (the `auto: cidx-meta refresh @ ...` commit right after the refresh completes), not the working tree.
