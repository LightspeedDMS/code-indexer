---
name: reference-corrupt-chunks-db-restore
description: "Runbook for a golden repo whose source chunks.db is \"database disk image is malformed\" (refresh fails every cycle) -- restore from the published snapshot under the alias lock"
metadata:
  node_type: memory
  type: reference
  originSessionId: 6fd11155-4f30-44aa-8f91-04eb0139fafb
  modified: 2026-10-03T15:22:14.617Z
---

Symptom: `background_jobs` shows one alias failing every refresh in phase `semantic` with
`Chunk store unavailable while writing ...: database disk image is malformed`. Queries keep
working but serve the last good `.versioned/` snapshot (stale). No self-heal exists until
Bug #2022 lands (the integrity-gate restore only runs after `cidx index` SUCCEEDS).

Detect: `PRAGMA quick_check` via `sqlite3.connect("file:<chunks.db>?mode=ro", uri=True)` on the
source repo's `.code-indexer/index/<model>/chunks.db`; also check the alias JSON `target_path`
snapshot (it is normally clean, since corruption happens before publish).

Fix (2026-10-03, ~5 min, only the delta gets re-embedded):
1. Hold the repo's own lock so the scheduler skips it, from the server's Python environment:
   `PostgresAliasLockStore(dsn).try_acquire(<repo name WITHOUT -global>, operation=...)`;
   release with `store.release(handle)` (the handle has no release method).
2. Move aside `index/<model>/`, `metadata-voyage-ai.json`, `tantivy_index/`,
   `indexing_progress.json` (keep as a forensic backup).
3. `cp -a --reflink=always` the same four from the snapshot. Run it on the storage host's
   local filesystem, because reflink does not work through the NFSv3 client. Then `chown -R` to
   the service user, quick_check, and release the lock.
4. Verify: the next two refreshes complete, and a front-door FTS search for a string unique to a
   post-outage file returns it. Semantic search with a single-file `path_filter` returns 0 even
   for indexed files (top-K first, path filter after), so it is not a valid probe.

Gotcha: `reclaimed job X (dead node: None)` is a RETURNING-after-UPDATE artifact. It is always
None and does not mean the job lacked an owner.

Related: [[feedback_verify_zero_json_chunks_on_indexing]], issue #2023 (incremental refresh
permanently misses files, so the source index can be incomplete even when healthy).
