# Cluster State and Background Job Invariants

Rules for state that must be visible across requests, workers and nodes, and for background jobs. Cluster design:
[Cluster Architecture](../cluster.md). Operator view of jobs: [Maintenance and Jobs](../../server/maintenance-and-jobs.md).
Index of all invariant groups: [README](README.md).

## Cluster-aware state

A request may land on any worker of any node, so state another request must see never lives in process memory
(module-level dicts, class-level dicts). Load-balancer affinity is not a substitute.

| State | Store |
|-------|-------|
| Cross-request payloads (search snippets, job results) | `app.state.payload_cache` (`PayloadCache`, `src/code_indexer/server/cache/payload_cache.py`; SQLite solo, PostgreSQL cluster; default TTL 900 s) |
| Job coordination and dedup | `JobTracker` / `background_jobs` table (PostgreSQL in cluster mode) |
| Configuration | `get_config_service().get_config()` (database-backed runtime settings) |
| Dep-map coordination | `SharedJobSentinel` files on the shared cidx-meta tree |

- `PayloadCache` methods: `store_with_key`, `has_key`, `retrieve`. On the query hot path use `store_batch(contents)`
  (one transaction; PostgreSQL `SET LOCAL synchronous_commit = off`), never `store()` in a loop.
- A PostgreSQL backend registered on `BackendRegistry` does nothing unless consumers use it. Construction sites
  resolve it with `resolve_backend_registry_attr(attr_name, caller_name=...)`
  (`src/code_indexer/server/utils/registry_factory.py`); check every `XCache(db_path)` / `XManager(db_path)`
  construction before declaring a backend wired.

## Background jobs

- Every background job runs through `BackgroundJobManager` and `JobTracker`
  (`src/code_indexer/server/repositories/background_jobs.py`) so it is visible on the dashboard and to admins.
- Per-repository dedup is `register_job_if_no_conflict`, backed by the partial unique index `idx_active_job_per_repo`
  on `(operation_type, repo_alias)` for pending or running rows with a non-NULL `repo_alias`. A job with `repo_alias=None` bypasses it and must dedup itself.
  Schedulers that must be fleet-singleton register under a fixed sentinel alias (`server`,
  `fleet-migration-scheduler`, `__depmap_dashboard__`).
- `_load_jobs_sqlite` never loads surviving running or pending rows into `self.jobs`: after the startup sweep they
  belong to another worker or node, and an in-memory copy would override the database in `list_jobs` /
  `get_job_status`.
- Cancellation reaches subprocesses only through the injected `cancel_check` (a database-backed check polled about
  every 2 s). Indexing children use `run_with_popen_progress(cancel_check=...)` (raises `IndexingCancelledError`);
  other subprocesses use `run_with_cancel` (`src/code_indexer/server/utils/cancellable_subprocess.py`, raises
  `SubprocessCancelledError`; plain `subprocess.run` when no check is given). Broad exception handlers on the refresh
  path re-raise when `_is_refresh_cancellation(e)` is true. Refresh cancellation points are listed in
  [Refresh Recovery](../refresh-recovery.md#cancellation).
- Refreshes run by `execute_refresh_for_claimed_job` (cluster-reclaimed refreshes executed by
  `DistributedJobWorkerService`) receive no `cancel_check`, so their subprocesses do not stop on cancel.
- A failed cancel-flag read never stops a job. It logs WARNING on the first and every `CANCEL_READ_WARN_EVERY` (30)
  consecutive failure and ERROR once at `CANCEL_READ_ESCALATE_AFTER` (150); a successful read resets the count.
- Process-group termination has one implementation, `src/code_indexer/utils/process_group.py`: it watches the whole
  group through the grace period, SIGKILLs survivors and never signals the caller's own group.

## Pod-pull work stealing (cluster mode)

- The memory-heavy operations in `POD_PULL_OPS` (`add_golden_repo`, `provider_index_add`,
  `provider_temporal_index_rebuild`, `sync_repository`, `change_branch`) are left pending in `background_jobs` with
  their parameters in the row's metadata, and are claimed by each node's `IndexJobClaimLoop` through the shared,
  memory-gated `DistributedJobClaimer` (`FOR UPDATE SKIP LOCKED`). The leader's `DistributedJobWorkerService` claims
  the other retryable types with `exclude_types=POD_PULL_OPS`, so each row has exactly one executor. Under memory
  pressure the gate also declines the leader's refresh claims.
- Progress of a stolen job is written to the shared row (`DistributedJobClaimer.update_progress`), so any node's
  dashboard shows it. `submit_job` keeps no in-memory entry for a pod-pull job.
- `add_golden_repo` removes a partial clone left by a crashed attempt (no committed row) before retrying
  (`_remove_orphan_clone_for_retry`).
- `validate_dispatch_covers(_index_dispatch, POD_PULL_OPS)` runs at startup and fails if an operation has no
  executor. The admission gates fail open when no memory governor is available.

## Repository discovery jobs

`POST /api/discovery/{platform}/start` and `GET /api/discovery/{platform}/result/{job_id}`
(`src/code_indexer/server/web/routes.py`) store the result in `app.state.payload_cache` under `discovery:{job_id}`,
never in a module-level dict. These jobs pass `repo_alias=None`, so the route deduplicates by scanning pending and
running jobs of the same operation type. Workers that report progress declare `progress_callback=None` for the job
manager to inject it.
