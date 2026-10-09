# Indexing and Migration Invariants

Rules for the indexing path, temporal indexing, HNSW integrity and database migrations. How indexing works:
[Indexing](../indexing.md). Chunk storage layouts and fleet migration: [Chunk Storage Invariants](chunk-storage.md)
and [Storage](../storage.md). What happens when a refresh's indexing fails: [Refresh Recovery](../refresh-recovery.md).
Index of all invariant groups: [README](README.md).

## No wall-clock timeouts on indexing

- Indexing, golden-repository registration and SCIP generation carry no timeout on the job, the subprocess, or any
  per-file or per-batch unit. A large repository legitimately takes hours.
- The only legitimate timeout on this path is the per-request outbound embedding HTTP call, with its retry and
  backoff (`voyage_ai.py`, `cohere_embedding.py`, `cohere_multimodal.py`, `provider_backoff.py`).
- No `future.result(timeout=...)` with skip-on-timeout on the per-file path (`file_chunking_manager.py`,
  `high_throughput_processor.py`, `temporal/temporal_indexer.py`). A post-retry embedding failure propagates and fails
  the job; never `except TimeoutError: skip`.
- Not a job clock, and kept: governor acquire, coalescer join, reranker timeouts, cancellation and shutdown signals,
  and the 30 s bounds on the git metadata commands used for progress estimates (`GIT_COMMAND_TIMEOUT_SECONDS` in
  `src/code_indexer/services/progress_subprocess_runner.py`).
- `run_with_popen_progress` also contains a progress-stall watchdog: given `stale_activity_timeout_seconds`, it kills
  a child that reports zero forward progress for that long and raises `IndexingWatchdogKillError`. Elapsed time alone
  never triggers it. The Web UI setting `indexing_watchdog_config.stale_activity_timeout_seconds` (default 120) exists,
  but no caller passes a value to `run_with_popen_progress`, so the watchdog is currently inactive.
- Fail loud on total failure: `cidx index` exits non-zero when `files_processed == 0 and failed_files > 0`, and
  `run_with_popen_progress` raises `IndexingSubprocessError` on a non-zero exit, so a registration whose indexing all
  fails fails the registration. A failed registration removes its own fresh clone (`_cleanup_failed_clone`) so a retry
  is not blocked.
- `cidx index` exits 86 or 87 on a fatal chunk-store failure; see [Refresh Recovery](../refresh-recovery.md).

## Temporal indexing

- One document per commit (`commit_aggregator.build_aggregated_document()`): the message once, then each changed
  file's diff. Point ids are `{project}:commit:{hash}:{j}`. Chunk overlap is per embedder adapter
  (`TemporalEmbedder.overlap_percentage`): 0 percent for `voyage-context-4`, 15 percent for `embed-v4.0`.
- Embedders are pluggable (`services/temporal/embedders/`, `register_embedder()` / `create_embedder()`).
  `TemporalConfig.embedders` selects which adapters build shards and `active_embedder` the default for queries. A
  per-query `temporal_embedder` never falls back to `active_embedder`: an embedder with no indexed collections returns
  an empty typed result.
- Shards are quarterly per embedder: `code-indexer-temporal-{model_slug}-{YYYY}Q{N}`.
- `voyage-context-4` (`services/temporal/embedders/contextual.py`): chunks are packed into request documents bounded
  by `_max_tokens_per_chunk` (the model's context window), not by the much larger request cap, because the provider
  rejects any single document over the context window. Query-purpose embeddings for this model go to the
  contextualized endpoint with `input_type="query"`, including the batch path the server coalescer uses
  (`VoyageAIClient.get_embeddings_batch`, `_CONTEXTUAL_QUERY_MODELS` in `services/voyage_ai.py`).
- Every server-side temporal `cidx index` child gets its environment from `build_temporal_child_env`
  (`server/storage/postgres/temporal_child_wiring.py`), in every storage mode; it sets `CIDX_SERVER_REFRESH_CONTEXT`,
  which selects the server temporal location.
- Incremental refresh is reconcile-based: a full `git log` walk every run, then a per-shard comparison against each
  shard's completed commits (`reconcile_temporal_index`). Do not add a last-indexed-commit cursor; it would skip
  partially completed commits.
- `TemporalIndexer._blank_out_legacy_collections()` deletes genuine legacy temporal monoliths but skips the shared
  bookkeeping directory (bare `code-indexer-temporal`), identified by having no vector data
  (`_is_shared_bookkeeping_directory`).
- Golden-repository temporal indexing covers only the registered branch. `all_branches` is gated by the runtime
  setting `temporal_all_branches_enabled` (default `False`). With the gate off, a request for `all_branches=true` is
  rejected at the REST registration, the Web UI temporal options form and the MCP `add_golden_repo` handler, and the
  three command builders drop `--all-branches` from a stored legacy `true` with a WARNING. The standalone CLI
  `--all-branches` flag is not affected.
- A global floor date (`TemporalIndexingConfig.index_floor_date`, runtime) bounds future temporal runs. Every launch
  site composes it with a repository's own `since_date` through `resolve_effective_floor_date()`
  (`src/code_indexer/server/services/temporal_floor_date.py`), which takes the later of the two. Lowering or clearing
  the floor makes the next run index the whole skipped backlog.
- Temporal reads in the server resolve the golden repository's lineage from the shared stores. The worker
  (`src/code_indexer/server/services/temporal_worker.py`) uses the injected `ActivatedRepoManager`; in PostgreSQL mode
  it raises `TemporalLineageStoreUnavailableError` rather than read node-local metadata. The predicate
  `uses_shared_metadata_stores()` checks capability (`is_shared_backend` on the golden-repo metadata backend), not mere
  presence. Both the MCP and the REST door pass the manager (guard:
  `tests/unit/server/services/test_temporal_doors_pass_activated_repo_manager_1533.py`).
  `is_postgres_storage_mode()` (`src/code_indexer/server/utils/registry_factory.py`) is the single storage-mode
  probe.
- Where temporal data lives in server context is described in
  [Chunk Storage Invariants](chunk-storage.md#temporal-data-location).

## HNSW orphan detection and repair

- Every HNSW build and finalize path (`HNSWIndexManager.build_index`, `rebuild_from_vectors`,
  `save_incremental_update`) calls `_detect_and_repair_orphans()` before persisting: integrity check, repair, re-check.
  A repair that does not reach zero orphans raises `HNSWIntegrityRepairError`.
- `check_integrity()` and `repair_orphans()` exist only in the project's hnswlib fork. When they are missing
  (`_hnswlib_has_fork_capability()` is false), the pass logs one WARNING and is skipped; the build still persists. The
  health service reports `hnswlib_capability_available` separately and never reports a false orphan error. Build
  instructions: [hnswlib custom build](../../server/hnswlib-custom-build.md).
- `orphan_count` in health output is binary: 0 is OK, anything above 0 is ERROR. There is no warning tier and no
  threshold.

## HNSW fleet orphan sweep

Package `src/code_indexer/server/services/hnsw_orphan_sweep/`, default on (`HNSWOrphanRepairSweepConfig`:
`enabled=True`, `batch_size=15`, `tick_interval_minutes=7`, optional UTC operating hours).

- Discovery reuses `list_golden_repos()` and `list_all_activated_repositories()`, finds `hnsw_index.bin` next to a
  `collection_meta.json`, and skips snapshot paths with the canonical predicate. Activated repositories are swept
  separately from their golden repository.
- Repair checks without a lock, then takes the same `.index_rebuild.lock` the builder uses, re-checks, repairs, writes
  atomically, verifies with a fresh reload and invalidates the server `HNSWIndexCache` entry. Races are transient
  skips.
- The durable cursor (`hnsw_orphan_sweep_state`) is a string sort key, never a numeric offset, and is persisted after
  each item.
- Single-flight only through `register_job_if_no_conflict` (one short background job per tick). It is not filtered by
  `ShardOwnership.owns()`.
- Fleet statistics: `GET /api/admin/hnsw-orphan-sweep/stats`.

## Database migrations

- Migrations must stay backward compatible so nodes on adjacent versions can share one schema during a rolling
  update. Allowed: `CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`, nullable or defaulted columns.
  Never: `DROP TABLE`, `DROP COLUMN`, renames, `ALTER COLUMN TYPE`, removing `NOT NULL`.
- PostgreSQL migrations are numbered files in `src/code_indexer/server/storage/postgres/migrations/sql/`, applied in
  order by `MigrationRunner` (`runner.py`) and recorded by filename in `schema_migrations` with an MD5 checksum. The
  checksum is recorded, not re-verified on later runs.
- `MigrationRunner.run()` holds a session-level advisory lock (`pg_advisory_lock`, key
  `_MIGRATION_ADVISORY_LOCK_KEY`, always passed as a `%s` parameter) for the whole run and releases it in `finally`,
  so concurrent workers apply each migration once. The SQLite path never references it.
- A column that is JSONB in PostgreSQL and TEXT in SQLite is read through
  `parse_json_column(raw, expected_type, field_name)` (`src/code_indexer/server/storage/json_column.py`), never a bare
  `json.loads()`.

## Discovery parity and import budget

- Incremental (git-diff) file discovery applies the same exclusion rules as the full walk by calling
  `FileFinder.matches_exclude_pattern()` (a pure string match, so deleted files classify correctly). Do not
  reimplement exclusion rules.
- Importing `FilesystemVectorStore` must not import `psycopg` or `fastapi`. Server-only imports are deferred with
  `TYPE_CHECKING` and PEP 562 `__getattr__` (guard: `tests/unit/storage/test_lazy_load_1468.py`, run in a subprocess).
