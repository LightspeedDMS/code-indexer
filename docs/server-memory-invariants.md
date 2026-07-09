# Server Memory Invariants

This document describes the server memory and FD hygiene invariants for production CIDX deployments.

**FD/connection hygiene**: A single `DatabaseConnectionManager-cleanup-daemon` thread runs for the app lifetime, sweeping stale SQLite connections across all registered singletons every 60s. Started/stopped in lifespan (error codes `APP-GENERAL-034`/`035`). Idempotent. Identity-guarded clear.

NEVER: re-introduce the piggyback cleanup trigger in `get_connection()` (lost races to thread churn in production, RC-3); call `_cleanup_all_instances()` from the daemon loop (double-throttle); remove the `try/finally` that calls `_close_thread_connections_on_all_managers(job_id)` in `BackgroundJobManager._execute_job` (Fix A.3 closes at source); remove the close-on-clobber guard in `get_connection()` (Linux TID recycling silently leaks FDs otherwise).

**HNSW/FTS cache cap**: Singletons always carry a finite `max_cache_size_mb`. When config has `None`, `DEFAULT_MAX_CACHE_SIZE_MB = 4096` is overlaid at `get_global_cache()` / `get_global_fts_cache()` init. Dataclass defaults stay `None` (sentinel distinguishes explicit operator value from unset). Hot-reload via `ConfigService._hot_reload_cache_size_cap()` is narrow-scoped to `index_cache_max_size_mb` and `fts_cache_max_size_mb` only — `TestHotReloadScopeIsolation` asserts the boundary.

**Omni fan-out mitigations**: two caps enforced in sequence — (1) `omni_wildcard_expansion_cap` (default 50, Web UI): per-pattern wildcard expansion cap, enforced inside `_expand_wildcard_patterns`, error code `wildcard_cap_exceeded`; (2) `omni_max_repos_per_search` (default 50, Web UI, Bug #894): total alias fan-out cap after wildcard expansion + literal union, enforced by `_enforce_repo_count_cap` in `_omni_search_code` and `_omni_regex_search`, error code `repo_count_cap_exceeded`. Both return `Union[List[str], CapBreach]` and callers handle via `cap_breach_response` / `cap_breach_http_exception`. Fan-out searches pass `hnsw_cache=None` to bypass the global HNSW cache; `sys.getsizeof(id_mapping)` added to `index_size_bytes` so label→id dict is no longer invisible to the size cap.

**glibc arena fragmentation mitigations** (both default ON since v9.23.3, bootstrap-only flags in `config.json`): After a bulk lifecycle backfill that cycles 500+ HNSW indexes through the LRU cache, process RSS can pin ~23 GB because glibc's multi-arena brk segments hold small `label_lookup_` / `linkLists_` allocations. Two mitigations behind feature flags (operators can disable either by setting to false in `config.json`):

- `enable_malloc_trim: bool = True` -- calls `glibc malloc_trim(0)` at the end of each `_cleanup_expired_entries()` cycle (implemented in `_maybe_malloc_trim()` in `hnsw_index_cache.py`). Linux + glibc only; silently no-ops on musl/macOS. Default ON since v9.23.3.
- `enable_malloc_arena_max: bool = True` -- idempotently injects `Environment=MALLOC_ARENA_MAX=2` into the cidx-server systemd unit file on each auto-updater run (`_ensure_malloc_arena_max()` in `deployment_executor.py`, step 6.6, error code `DEPLOY-GENERAL-143`). Reverting the flag removes the line on the next auto-updater cycle. Default ON since v9.23.3.

Both flags are bootstrap-only (read from `config.json` before DB is available) and default True since v9.23.3 so fresh installs and existing installs that don't pin the flags automatically get the protection. Tests: `tests/unit/server/cache/test_malloc_trim_flag_bug_897.py`, `tests/unit/server/auto_update/test_malloc_arena_max_bug_897.py`.

Files: `src/code_indexer/server/storage/database_manager.py`, `src/code_indexer/server/startup/lifespan.py`, `src/code_indexer/server/repositories/background_jobs.py`, `src/code_indexer/server/cache/__init__.py`, `src/code_indexer/server/services/config_service.py`, `src/code_indexer/server/mcp/handlers/_utils.py`, `src/code_indexer/server/cache/hnsw_index_cache.py`. Tests: `tests/unit/server/mcp/test_wildcard_cap.py`, `test_cap_breach_helper.py`, `test_repo_count_cap.py`, `test_cache_bypass_on_fanout.py`, `tests/unit/server/cache/test_id_mapping_size_bytes.py`.

Operational check:
```bash
sqlite3 ~/.cidx-server/logs.db \
  "SELECT timestamp, message FROM logs WHERE message LIKE '%cleanup daemon%' ORDER BY timestamp DESC LIMIT 5;"
# Expect one 'started' per process + periodic 'Cleaned up N stale SQLite connections' under churn.
```
