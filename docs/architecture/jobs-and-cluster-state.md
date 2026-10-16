# Jobs and Cluster State

Maintainer reference for how the server runs long operations as background jobs, how jobs are deduplicated,
distributed and cancelled across cluster nodes, and where state that must be visible to another request or node is
stored. Operator-facing job management (dashboard, cancel, maintenance drain) is in
[server/maintenance-and-jobs.md](../server/maintenance-and-jobs.md); cluster topology is in [cluster.md](cluster.md).

All paths below are relative to `src/code_indexer/`.

## Contents

- [The rule: no per-node RAM for cross-request state](#the-rule-no-per-node-ram-for-cross-request-state)
- [Job model](#job-model)
- [Submission and deduplication](#submission-and-deduplication)
- [Cluster distribution](#cluster-distribution)
- [Cancellation](#cancellation)
- [Process-group termination](#process-group-termination)
- [Jobs outside BackgroundJobManager](#jobs-outside-backgroundjobmanager)

## The rule: no per-node RAM for cross-request state

In a cluster, HAProxy spreads requests across nodes, and with `--workers N` a node runs several processes. A module or
class level dict, or any other process memory, is visible only to the process that wrote it. Any state that a later
request may need to read must live in a shared store:

| State | Store | Module |
|-------|-------|--------|
| Payloads referenced by a later request (truncated search results, X-Ray pages, discovery results) | `PayloadCache`, `app.state.payload_cache`: SQLite in solo mode, PostgreSQL `payload_cache` table in a cluster, entries expire after a TTL | `server/cache/payload_cache.py` |
| Job status, progress, cancellation, dedup | `background_jobs` table through `JobTracker` / the jobs storage backend | `server/services/job_tracker.py`, `server/repositories/background_jobs.py` |
| Settings | the committed runtime configuration row | `server/services/config_service.py`, see [config-state.md](config-state.md) |
| Cluster-wide "one at a time" claims for dependency-map work | `SharedJobSentinel` files on the shared `cidx-meta` storage | `server/services/shared_job_sentinel.py`, see [dependency-map.md](dependency-map.md) |
| Write exclusion on a golden repository | `RefreshScheduler.acquire_write_lock()` (lock file or database store) | `global_repos/alias_lock_coordinator.py` |

Session affinity in the proxy is not a substitute: correctness must not depend on proxy configuration. Process
memory is still fine for data that is only a cache of shared state, or that belongs to work the process itself is
executing (for example `BackgroundJobManager.jobs`, below).

`PayloadCache` writes on the query path use `store_batch()` (one transaction for all items of a response);
`store_with_key()` / `has_key()` / `retrieve()` serve keyed results such as discovery outputs.

## Job model

Two cooperating components share the `background_jobs` table (SQLite in solo mode, PostgreSQL in a cluster):

- **`JobTracker`** (`server/services/job_tracker.py`) is the record of every tracked operation: `register_job`,
  `register_job_if_no_conflict`, `update_status`, `complete_job`, `fail_job`, `cancel_job`, `get_active_jobs`,
  `query_jobs`. It keeps an in-memory index of active jobs for fast lookup and persists every transition.
- **`BackgroundJobManager`** (`server/repositories/background_jobs.py`) executes jobs. Its `jobs` dict holds only the
  jobs this process is executing. Listing (`list_jobs`) queries the database first and overlays fresher in-memory
  progress for local jobs. At startup it sweeps orphaned rows and deliberately does not load other processes' active
  rows into memory, so a stale local copy can never override the database.

**Lanes.** Jobs run on persistent worker threads in two pools: `ordinary`
(`background_jobs_config.max_concurrent_background_jobs`, default 5) and `temporal`
(`temporal_lane_concurrency`, default 2, range 1 to 32). Both are runtime settings. Memory-heavy operations
(`add_golden_repo`, `provider_index_add`, `provider_temporal_index_rebuild`, `sync_repository`, `change_branch`) are
deferred by a memory-aware admission gate while the node is under memory pressure.

**Worker function contract.** `_execute_job` inspects the job function's signature and injects `job_id`,
`progress_callback` and `cancel_check` when the function declares them.

## Submission and deduplication

`BackgroundJobManager.submit_job(operation_type, func, ..., submitter_username, repo_alias, lane, metadata)`:

1. Refuses new jobs in maintenance mode (`MaintenanceModeError`).
2. When `repo_alias` is given and a tracker exists, registers the job with
   `JobTracker.register_job_if_no_conflict()`. The insert relies on the partial unique index
   `idx_active_job_per_repo` on `(operation_type, repo_alias) WHERE status IN ('pending', 'running') AND repo_alias
   IS NOT NULL` (PostgreSQL migration `004_active_job_unique_constraint.sql`; SQLite in
   `server/storage/database_manager.py`). A second active job for the same pair, on any node, fails the insert and
   raises `DuplicateJobError` carrying the existing job id, which callers may join. There is no read-then-write
   window.
3. Without a `repo_alias` there is no conflict check: the job is registered with `register_job()`. When a
   `repo_alias` is given but no `JobTracker` exists, an in-process conflict check runs instead, which deduplicates
   only within the process. Callers that need cluster-wide dedup must pass `repo_alias`.
4. Queues the job on its lane.

`JobTracker.check_operation_conflict()` followed by `register_job()` is a check-then-act sequence and is not
cluster-safe; new code uses `register_job_if_no_conflict()`.

## Cluster distribution

- **Pod-pull work stealing.** In cluster mode, a memory-heavy operation (`POD_PULL_OPS`) submitted with a
  `repo_alias` and reconstruction `metadata` is left `pending` in the shared queue with no executing node, instead of
  running on the submitting node. `IndexJobClaimLoop` (`server/services/index_job_claim_loop.py`), running on every
  PostgreSQL node, claims such rows through the memory-gated `DistributedJobClaimer`
  (`server/services/distributed_job_claimer.py`; `FOR UPDATE SKIP LOCKED`, so exactly one node claims a row) and
  rebuilds the work from `metadata`.
- **Leader re-execution.** `DistributedJobWorkerService` (`server/services/distributed_job_worker.py`) runs on the
  leader only, polls every 30 s, and re-executes reclaimed refresh jobs (`global_repo_refresh`,
  `refresh_golden_repo`); it excludes `POD_PULL_OPS`.
- **Restart cleanup.** Orphaned rows are cleaned per node at startup (`JobTracker.cleanup_orphaned_jobs_on_startup`);
  the unscoped SQLite sweep runs only in the primary process.

## Cancellation

Cancellation is a status in the database, so a cancel issued on any node reaches the node executing the job:

1. `BackgroundJobManager.cancel_job()` marks a local job cancelled (a pending job becomes `cancelled` at once). For a
   job not in this process's memory it writes the cancellation to the database.
2. The executing node notices through `_check_db_cancellation()`, called from the job's progress callback and from
   the injected `cancel_check()`, which reads the job row. A failed read never stops the job: the 1st and every 30th
   consecutive failure log a WARNING, the 150th logs one ERROR (`CANCEL_READ_WARN_EVERY`,
   `CANCEL_READ_ESCALATE_AFTER`), and a successful read resets the streak.
3. Long subprocesses honour the check while they run:
   - `run_with_cancel()` (`server/utils/cancellable_subprocess.py`) behaves like `subprocess.run`, but starts the
     child in its own session and polls `cancel_check()` every 2 s; on cancel it terminates the process group and
     raises `SubprocessCancelledError`. Without a check it is exactly `subprocess.run`.
   - `run_with_popen_progress(cancel_check=...)` (`services/progress_subprocess_runner.py`) does the same for
     indexing subprocesses that stream progress and raises `IndexingCancelledError`.

   Neither adds a wall-clock limit: indexing and refresh work has no job, subprocess or per-file timeout.
4. Child processes registered with `register_child_process()` (X-Ray `xray-cli` children) are terminated when their
   job is cancelled.

A golden-repository refresh re-checks cancellation before indexing, after indexing, after the integrity gate, before
creating the snapshot and before the alias swap; a snapshot created but not published is scheduled for cleanup and
the job ends `cancelled`. Recovery after failed refreshes is described in [refresh-recovery.md](refresh-recovery.md).

Refreshes re-executed by `DistributedJobWorkerService` (`execute_refresh_for_claimed_job`) receive no
`cancel_check`, so their subprocesses cannot be stopped by a cancel.

## Process-group termination

`terminate_process_group()` (`utils/process_group.py`) sends `SIGTERM` to the child's process group, watches the
whole group for a grace period (default 2 s), then sends `SIGKILL` to every survivor, and waits for the direct child.
It refuses to signal the caller's own process group. `cancellable_subprocess.py`, `progress_subprocess_runner.py` and
`activity_watchdog.py` use it.

Three other paths terminate children themselves: `RustNativeBackend` sends `SIGKILL` to the `xray-cli` process group
on timeout (`xray/rust_backend.py`), `CodexInvoker` does the same on timeout (`server/services/codex_invoker.py`), and
`BackgroundJobManager._terminate_child_processes()` signals each registered child process (not its group) with
`terminate()` and then `kill()`.

## Jobs outside BackgroundJobManager

Some operations are tracked but not executed by `BackgroundJobManager`:

- `xray_search` and `xray_explore` register with `JobTracker.register_job()` without a repository (so concurrent
  read-only searches on one repository are not serialized) and run on the dedicated `xray_executor`
  (`server/mcp/handlers/xray/`).
- `analyze_graph` runs synchronously within its timeout, off the event loop, and is not a job.
- Dependency-map analyses claim a `SharedJobSentinel` before their background thread starts and register their job row
  through `JobTracker`.

See [xray/architecture.md](xray/architecture.md) and [dependency-map.md](dependency-map.md).
