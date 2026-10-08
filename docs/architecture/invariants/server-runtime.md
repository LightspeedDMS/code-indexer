# Server Runtime Invariants

Rules for how the server process is built and how it bounds memory, file descriptors and connections. Index of all
invariant groups: [README](README.md).

## Module-level singletons

- Never bind a heavy service to a bare module-level name that is constructed at import time. Importing a handler
  module must not start threads or load databases.
- A PEP 562 module `__getattr__` that defers the binding is necessary but not sufficient: `from module import name`
  also triggers it, so a consumer's module-scope import still forces construction. The real fix is a cheap
  constructor: expensive sub-objects become lazy properties guarded by a class-level `threading.RLock` (re-entrant, and
  class-level so instances created with `Cls.__new__` still have it).
- Current examples:
  - `src/code_indexer/server/services/git_operations_service.py`: both layers (lazy properties with class-level
    `RLock`s, and a module `__getattr__` with `_lazy_init_lock`, `_lazy_values`).
  - `src/code_indexer/server/services/file_service.py`: cheap constructor only (lazy `activated_repo_manager`
    property behind a class-level `RLock`); the module binds `file_service = FileListingService()` eagerly, which is
    safe because construction is cheap.
  - `src/code_indexer/server/app.py`: module `__getattr__` only (below).
- Verify a change with the import-has-no-side-effects test for that module plus a re-entrancy test.

## Lazy app construction

- A bare `import code_indexer.server.app` is inert: no `ConfigService`, no database I/O, no singleton registration, no
  lock contention.
- `app` and the service globals listed in `_LAZY_INIT_ATTRS` are declared annotation-only; the module `__getattr__`
  calls `create_app()` once, under `_lazy_init_lock` (an `RLock`), the first time one of them is accessed. That is how
  `uvicorn code_indexer.server.app:app` starts the server. `_initialized` / `_initializing` stop a re-entrant call
  during first boot from running `create_app()` again; `_lazy_values` keeps the values for names `create_app()` does
  not assign.
- Do not add a module-level service global to `app.py` with a default binding; add it to `_LAZY_INIT_ATTRS`.
- Modules in `create_app()`'s own import chain (`startup/service_init.py`, `startup/lifespan.py`,
  `startup/app_wiring.py`, `auth/dependencies.py`) import the lazy names only inside functions.
- Guards: `tests/unit/server/test_app_import_no_side_effects_1638.py`, `tests/unit/server/test_app_lazy_init_repair_1638.py`.

## Server memory and pooling

- SQLite connection hygiene (`src/code_indexer/server/storage/database_manager.py`): one
  `DatabaseConnectionManager-cleanup-daemon` thread per app lifetime sweeps stale per-thread connections every 60 s;
  it is started and stopped in `startup/lifespan.py` (`APP-GENERAL-034` / `035` on failure). Never trigger cleanup from
  `get_connection()`, never call `_cleanup_all_instances()` from the daemon loop, keep the close-on-clobber check in
  `get_connection()` (Linux reuses thread ids), and keep the `try/finally` in `BackgroundJobManager._execute_job` that
  closes the job thread's connections (`_close_thread_connections_on_all_managers`).
- HNSW and FTS caches always have a finite size cap: `DEFAULT_MAX_CACHE_SIZE_MB = 4096`
  (`src/code_indexer/server/cache/__init__.py`) is applied when the setting is unset. `initialize_caches(worker_count)`
  divides the per-node cap by the worker count, with a floor of `MIN_CAP_PER_WORKER_MB = 256`. It is called once, in
  `initialize_services()` (`startup/service_init.py`), before the eager cache getters; do not add a second call in
  `lifespan.py`. The worker count is the applied one (`applied_worker_count.get_applied_worker_count()`: the live
  systemd `ExecStart --workers`, then `applied_launch.json`, then 1), not a saved but unapplied target.
- An HNSW cache entry's size includes `sys.getsizeof(id_mapping)`, so the label-to-id map counts toward the cap
  (`server/cache/hnsw_index_cache.py`).
- Only the HNSW and id-index caches hold loaded indexes for queries. The FTS cache singleton is built at startup, but
  the server FTS query paths open a fresh `TantivyIndexManager` per query and never read it, so a reindex needs no FTS
  cache invalidation.
- Hot reload of cache settings is limited to `index_cache_max_size_mb` and `fts_cache_max_size_mb`
  (`ConfigService._hot_reload_cache_size_cap()`); entries over a lowered cap are evicted at once.
- Omni-search fan-out is capped twice: `omni_wildcard_expansion_cap` (default 50, error `wildcard_cap_exceeded`) and
  `omni_max_repos_per_search` (default 50, error `repo_count_cap_exceeded`, `_enforce_repo_count_cap`). Fan-out
  searches bypass the global HNSW cache (`hnsw_cache=None`).
- glibc arena fragmentation: the bootstrap flags `enable_malloc_trim` (calls `malloc_trim(0)` after each HNSW cache
  cleanup cycle; Linux/glibc only) and `enable_malloc_arena_max` (the auto-updater adds `Environment=MALLOC_ARENA_MAX=2`
  to the unit) both default to `True`.
- Production embedding HTTP uses one long-lived keep-alive `httpx.Client` owned by `HttpClientFactory`
  (`create_sync_client(pooled=True)`), closed once at shutdown (`close_pooled_clients()`). Authentication is passed
  per request, so key rotation needs no client rebuild. With fault injection on, pooling is bypassed.
- `api_metrics_service` drains its queue in batches and writes each drain with one `upsert_buckets_batch()`
  transaction.
- Check the cleanup daemon from the logs database:

  ```bash
  sqlite3 ~/.cidx-server/logs.db \
    "SELECT timestamp, message FROM logs WHERE message LIKE '%cleanup daemon%' ORDER BY timestamp DESC LIMIT 5;"
  ```

## Multi-worker throughput benchmark

`scripts/analysis/multi_worker_throughput.py` measures `POST /api/query` throughput in four scenarios (repeating or
unique queries, cache on or off) per worker count, and exits 1 when the 2-worker/1-worker ratio is below
`--regression-multiplier` (default 1.7). It never starts or stops a server: point `--server` at an isolated server,
never the development server on port 8000. Credentials come from the environment or `--local-testing`. Reports go to
`reports/perf/`. The full 1, 2, 3, 4 worker run is a manual operator gate, not CI; the pytest wrapper
`tests/performance/test_multi_worker_scaling.py` is skipped unless `CIDX_PERF_TEST=1`.

```bash
python3 scripts/analysis/multi_worker_throughput.py --help
python3 scripts/analysis/multi_worker_throughput.py \
  --server http://localhost:8105 --workers 1,2,3,4 --queries 200 --concurrency 20
```
