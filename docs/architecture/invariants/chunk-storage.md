# Chunk Storage and Fleet Migration Invariants

Rules for the two chunk storage layouts, the in-place migration between them, and where temporal data lives. The
layouts and data flow are described in [Storage](../storage.md). Index of all invariant groups: [README](README.md).

## Layout authority

- Two layouts are supported side by side: SHARDED_JSON (one `vector_*.json` file per chunk, hash-sharded directories,
  plus `id_index.bin`) and CHUNKS_DB (one SQLite `chunks.db` per collection, `ChunkStore` in
  `src/code_indexer/storage/sqlite_chunk_store.py`).
- `resolve_chunk_layout(collection_dir)` in `src/code_indexer/storage/shared/chunk_layout.py` is the only function
  that decides a collection's layout. Never probe for `chunks.db` directly.
- It reads a top-level `chunks_db` key in `collection_meta.json`. The valid value is an object with an integer
  `version >= 1` (`{"chunks_db": {"version": 1}}`; a boolean does not count); anything absent or invalid resolves to
  SHARDED_JSON. It never raises.
- `write_chunks_db_discriminator()` is the final step of a build: called only after `chunks.db`, the HNSW index and
  the path index are durable. It requires `collection_meta.json` to exist and writes it atomically (temporary file,
  fsync, `os.replace`, directory fsync), because that file also holds the HNSW `id_mapping`.
- Write and finalize sites must use `FilesystemVectorStore._is_chunks_db_collection()`, which also honours the
  in-session build intent. The bare resolver reports SHARDED_JSON for a fresh build until the discriminator is
  committed, which misclassifies it.
- `id_index.bin` is never read or written for a CHUNKS_DB collection; point ids resolve through the `ChunkStore`
  primary key. Its absence there is the expected state, and `cidx status` reports it as not applicable.
- In `search()`, the HNSW load may run on a worker thread, but layout resolution and the `ChunkStore` open for
  hydration happen on the calling thread after the load returns. SQLite connections are not shared across threads.
- The scalar fields `vector_count`, `unique_file_count` and `points_count` in `collection_meta.json` are trusted by
  readers without re-derivation; any CHUNKS_DB writer change must keep them accurate.
- Any side effect the legacy write path performs must also be performed by `_upsert_points_chunks_db` (for temporal
  collections: the temporal metadata batch, and skipping per-file git blob lookups).

## Layout of new collections

- The CLI and daemon build new semantic collections as SHARDED_JSON unless `--new-collection-layout=chunks_db` is
  given or `CIDX_CHUNKS_DB_NEW_COLLECTIONS` is truthy. `cidx index --clear` rebuilds every collection as CHUNKS_DB and
  rejects an explicit `sharded_json`.
- The server passes `--new-collection-layout=chunks_db` to every server-side `cidx index` child
  (`SERVER_NEW_COLLECTION_LAYOUT_ARG` in `src/code_indexer/server/utils/index_command_layout.py`).
- Temporal collections are always built as CHUNKS_DB: the CLI refuses `--index-commits` together with
  `--new-collection-layout=sharded_json` (`reject_sharded_json_for_temporal` in `cli.py`), the server always passes
  `chunks_db`, and the daemon refuses legacy temporal shards.
- An existing collection's committed layout always wins over these choices.

## Read-only inspection

Every existence, health or status check routes through `resolve_chunk_layout()` and, for CHUNKS_DB,
`chunk_store_has_real_data(db_path, on_error=...)` in `sqlite_chunk_store.py`. It opens the file read-only through a
properly escaped `file:` URI, never creates a missing file, and counts rows directly. "No such table" or a genuinely
absent file means no data; every other SQLite or OS error follows `on_error` (`"treat_absent"` logs and returns
`False`; `"raise"` re-raises, used where the answer authorises a deletion). Never use a bare `rglob("*.json")`: every
collection contains `collection_meta.json`.

Temporal status call sites (repository health, activated-repository status, Web UI index flags, dashboard, activated
index manager) all use `get_temporal_repo_status()` (`src/code_indexer/services/temporal/temporal_status.py`), which
scans both temporal roots (the server's fixed root and the in-repository location). `has_data` (any shard holds
committed rows) and `is_queryable` (at least one shard has a working HNSW index) are distinct; a shard can have data
before it is queryable. Never conflate them.

## In-place consolidation

`consolidate_collection_in_place(collection_dir, *, deletion_authorized=True)` in
`src/code_indexer/storage/shared/collection_migration.py`:

1. scans the legacy files with the side-effect-free `IDIndexManager.scan_vectors_for_id_map()` (never
   `rebuild_from_vectors()`, which writes `id_index.bin`);
2. writes `chunks.db` as a pure addition, after a disk-headroom check that skips the collection when space looks
   insufficient;
3. reads every record back and compares it field by field, raising `ConsolidationVerificationError` on a mismatch;
4. commits the discriminator;
5. only then, and only when `deletion_authorized`, deletes the legacy files, the empty shard directories and
   `id_index.bin`, never the collection root.

It is idempotent and crash-safe: before step 4 a rerun starts over; after it, a rerun goes straight to cleanup. A
collection built natively as CHUNKS_DB (discriminator, `chunks.db`, no manifest, no legacy files, no authoritative
`vector_count`) passes integrity verification and counts as consolidated.

`enumerate_migration_targets()` (`src/code_indexer/services/chunk_migration_cli.py`) never selects a temporal
directory without `collection_meta.json`: the bare `code-indexer-temporal` name and the quarter-less per-embedder
bookkeeping directory (`code-indexer-temporal-{slug}`) are skipped, or reported as an anomaly if they hold vector data.

Temporal reconciliation on a CHUNKS_DB shard deletes stray points with
`ChunkStore.delete_stray_points_fail_closed()` (`sqlite_chunk_store.py`): one transaction with
`PRAGMA synchronous=FULL`, rolled back on any failure, never a partial delete.

## Fleet migration

Package `src/code_indexer/server/services/fleet_migration/`.

- Off by default: `fleet_migration_config.enabled = False`, `tick_interval_minutes = 30`, and an optional
  `canary_gate_enabled` that pauses the fleet after the first repository of a sweep until
  `FleetMigrationScheduler.confirm_canary()`. A configuration read error counts as disabled.
- Both destructive primitives are gated by `deletion_authorized`, which `run_fleet_migration_for_repo(...)` resolves
  from `fleet_migration_config.enabled` immediately before the migration sequence. No code verifies that every node
  runs the dual-layout reader; the operator confirms each node's `server_version` (dashboard node panel) before
  enabling.
- One repository per job, fleet-wide: jobs are submitted under the fixed `repo_alias` `fleet-migration-scheduler`,
  so `register_job_if_no_conflict` serialises the fleet. A repository that fails
  `FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD` (3) consecutive times is quarantined so it cannot starve the others.
- The orchestrator takes the repository's write lock directly with `MIGRATION_LOCK_TTL_SECONDS` (24 h) and checks
  `check_refresh_not_in_progress()` before touching the base clone; a running refresh makes it return
  `refresh_in_flight`. Activation during migration fails fast on the same lock.
- After a repository passes the completion gate (`verify_collection_fully_migrated` for every semantic collection,
  `repo_temporal_dirs_fully_consolidated` for temporal), it publishes a snapshot with the low-level snapshot and alias
  primitives (`trigger_post_consolidation_snapshot`) and applies the normal retention.
- Migration status is derived from disk on every call (`is_repo_already_migrated`); there is no cursor table.

## Cache keys and deactivation

- The shared HNSW and id-index cache key is `_activation_scoped_cache_key(path, chunk_layout_token=...)`: the path,
  plus the collection's resolved layout, plus the activation's `activation_id` (a UUID stored with the activation;
  PostgreSQL migration `039_activated_repos_activation_id.sql`). A consolidation or a reactivation at the same path is
  therefore a cache miss. Code that invalidates an entry must build the key with
  `FilesystemVectorStore.hnsw_cache_key_for_collection()`.
- Deactivation waits up to `deactivation_query_drain_max_wait_seconds` (default 30, runtime) for in-flight queries on
  the repository (`wait_for_activated_repo_query_drain`), then proceeds with a WARNING. Activated-repository searches
  increment and decrement the QueryTracker under the same key the drain polls.

## Temporal data location

- `src/code_indexer/services/temporal/temporal_server_paths.py` is the only module that decides where temporal data
  lives. `resolve_temporal_index_dir(codebase_dir)` returns the in-repository path outside server context; in server
  context (`CIDX_SERVER_REFRESH_CONTEXT`, set by `build_temporal_child_env` for every server temporal child) it returns
  the fixed root from `server_temporal_index_root()`:
  `{golden_repos_dir}/.temporal/{alias}/code-indexer-temporal-{embedder}-{quarter}/`. An unrecognised layout in server
  context raises `ValueError`; it never falls back to the in-repository path.
- Reads derive the location from the golden alias, never from an activation's clone, and fail closed when the alias
  is known but the root cannot be derived. Activated repositories never own temporal data.
- Refreshes write the fixed path in place. Readers never see a torn artifact because `ChunkStore` writes in SQLite
  transactions and HNSW is published by atomic rename. Do not add a scratch-and-swap layer: the temporal metadata key
  is derived from the collection path.
- Because the path does not change across refreshes, `HNSWIndexCache` detects a republished index by file identity: the fingerprint
  `(st_mtime_ns, st_size, st_ino, st_dev)` is captured before loading. The freshness `stat` runs with no lock held,
  at most one thread per key, no more often than `_FRESHNESS_RECHECK_MIN_INTERVAL_SECONDS` (2 s); when it cannot run,
  the last verified graph is served with one WARNING per degradation. Keep publishing `hnsw_index.bin` by atomic
  rename: the inode is what makes detection exact.
- The CLI migrates legacy JSON temporal shards in place before any temporal write
  (`consolidate_legacy_temporal_shards`) and aborts indexing if that fails. The daemon refuses instead
  (`legacy_temporal_refusal_response` in `daemon/service.py`), because migration needs the repository's exclusive
  lock and can run for hours. The refusal must use the CLI delegation contract, `status="error"` plus `message`: any
  other shape (for example `status="failed"` with `error`) is rendered without its reason.
- Shards written in-repository before the fixed root existed are relocated by `TemporalLegacyMigrationScheduler`
  (`src/code_indexer/server/services/temporal_legacy_migration/`), gated by
  `temporal_legacy_migration_config.relocation_enabled` and `cleanup_authorized` (both default `False`) and run under
  the repository's refresh-safe write lock.
- Retired, do not reintroduce: the versioned "sister location" placement (`TemporalShardResolver`,
  `maybe_relocate_shard_to_sister_location`, `bootstrap_temporal_namespace_to_sister` from production paths). Its
  relocation trigger read only `vector_*.json` and would publish an empty version over a `chunks.db` shard.
  `discover_and_enforce_temporal_retention` (`src/code_indexer/global_repos/snapshot_retention.py`) still deletes
  superseded directories behind any leftover `{alias}-temporal-*.json` pointers; it reads no temporal data.
