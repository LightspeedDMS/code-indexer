# Refresh Failure Recovery

What the server does when a golden-repository refresh cannot publish: how an indexing failure is classified, when a
corrupt chunk store is restored, how repeated failures back off and quarantine, and how deferred refresh requests are
kept and settled. Audience: maintainers and contributors. The refresh flow itself (fetch, index, snapshot, alias
swap) is in [Repository Lifecycle](repository-lifecycle.md#refresh-and-publication).

Main modules:

- `src/code_indexer/global_repos/refresh_scheduler.py`: the refresh cycle (`RefreshScheduler._execute_refresh`,
  `_index_source`).
- `src/code_indexer/global_repos/refresh_failure_recovery.py`: self-heal, strikes, backoff, deferred triggers.
- `src/code_indexer/global_repos/refresh_integrity_gate.py`: durability flush and `PRAGMA integrity_check` before
  publishing.
- `src/code_indexer/services/index_failure_exit_codes.py`: the exit-code contract between `cidx index` and the server.

All recovery state lives in the golden-repository metadata store (SQLite in solo mode, PostgreSQL in cluster mode);
nothing is per node.

## The refresh cycle and where it can stop

```
begin_refresh_cycle            read backoff/strike state and the trigger generation
fetch / pull base clone
_index_source                  cidx index child on the mutable base clone
   fatal chunk-store failure -> self-heal, stay failed, nothing published
integrity gate                 flush + integrity_check every chunks.db
   failure                   -> strike or backoff, publish skipped
_create_snapshot               CoW copy of the base clone into .versioned/
swap_alias                     publish
resolve_after_publish          clear backoff, settle covered triggers
```

The previously published snapshot keeps serving queries whenever a cycle stops before `swap_alias`.

## Classifying a fatal chunk-store failure

The server runs `cidx index` as a child process. When the child fails on a fatal chunk-store error it exits with a
reserved code (`index_failure_exit_code()` in the CLI's failure path):

| Exit code | Kind | Meaning | Server reaction |
|-----------|------|---------|-----------------|
| 86 | `CORRUPTION` | SQLite itself reported the store damaged (SQLITE_CORRUPT or SQLITE_NOTADB), decided by an allow-list (`sqlite_error_reports_corruption`), including a bare corruption report on a read path | integrity check, then restore only confirmed-corrupt collections |
| 87 | `ENVIRONMENT` | any other fatal store failure: I/O error, locked, busy, disk full, read-only, permission, other OS error | nothing restored; back off |
| 1 | generic | any other failure | ordinary failed refresh |

Classification is done from the exception chain in the child, never by parsing stderr in the parent. The parent maps
the code back with `chunk_store_failure_kind_for_exit_code()` and raises `FatalChunkStoreIndexError(kind)`. A child
terminated by SIGTERM (server shutdown) is reported as an interrupted refresh instead.

## Self-heal after exit 86

`self_heal_after_fatal_chunk_store_failure()` runs while the refresh still holds its publish write lock and never
publishes; the caller re-raises the original error, so the cycle stays failed.

1. Check that the refresh still owns its write lock (`verify_ownership`).
2. For `ENVIRONMENT`, record one backoff failure and stop.
3. For `CORRUPTION`, run the integrity gate on the base clone's index, with the currently published snapshot as the
   restore source. A collection is restored only when its own check completed and reported damage; a check that could
   not run is inconclusive and restores nothing. The restore source is itself integrity-checked first, and the
   restored file is checked again. Ownership of the write lock is re-checked immediately before every restore copy.
4. If at least one collection is confirmed corrupt, the cycle records one strike, however many collections are
   affected (`record_integrity_strike`). If anything remains unrepaired or inconclusive, a backoff failure is
   recorded too.
5. If every collection passes the check despite the exit code, nothing is restored and a backoff failure is recorded.

With no published snapshot yet (first refresh) there is nothing to restore from.

## Integrity gate before publishing

After `_index_source` succeeds, `run_refresh_integrity_gate()` flushes every CHUNKS_DB collection durably
(`ChunkStore.flush_durable()`: fsync of the file and its directory) and runs `PRAGMA integrity_check` on a fresh
read-only connection. A failure skips the snapshot and swap and attempts the same reflink restore from the published
snapshot. Bookkeeping (`record_publish_gate_failure`): a strike only for confirmed corruption; an inconclusive check
records a backoff instead, so transient I/O never quarantines a healthy repository. A SHARDED_JSON-only repository
passes trivially.

## Strikes and quarantine

`REFRESH_INTEGRITY_QUARANTINE_THRESHOLD = 3` consecutive strikes quarantine the alias: every refresh cycle of it
skips indexing (result `integrity_quarantined`) until an operator investigates. If the quarantine state cannot be
read, the cycle also skips indexing (`quarantine_check_failed`) rather than treat the repository as healthy. A passing
gate resets the count (`reset_integrity_strikes`).

## Persisted backoff

- `record_failure_backoff` increments `consecutive_failure_count` in `refresh_failure_backoff_state` for every
  failure the self-heal did not repair. The delay is `300 s * 2^(failures - 1)`, capped at 21600 s
  (`FAILURE_BACKOFF_BASE_SECONDS`, `FAILURE_BACKOFF_CAP_SECONDS`).
- The backoff applies only to system-submitted refreshes (`submitter_username == "system"`). A user-requested refresh,
  a forced reset, and externally managed mode (where no scheduler loop runs to fire a deferral) are not deferred.
- On the git schedule a due alias inside its backoff gets its `next_refresh` moved to the backoff end
  (`defer_due_alias`).
- While a backoff row or strikes exist, a cycle re-gates and must publish: it cannot end as "No changes detected"
  (`begin_refresh_cycle` returns `regate`).
- Only a verified publish clears the backoff (`resolve_after_publish`). Removing the repository deletes the row.

## Deferred triggers

A system trigger that arrives during a backoff is deferred, never dropped:

1. `defer_if_backed_off` marks the row `pending_trigger` with a due time at the backoff end and raises
   `RefreshDeferredError`, a `DuplicateJobError` subclass, so every system caller already treats it as "a refresh will
   happen later". If a concurrent publish deleted the row meanwhile, the mark reports it and the trigger is submitted
   normally.
2. Every scheduler loop pass calls `fire_expired_deferred_triggers`: it lists due triggers through an index, leases
   each one atomically for its backoff interval (only one scheduler wins), and submits it. The trigger stays pending
   until a verified publish resolves it, so process death, an orphaned job or a failed submission only delay it to the
   end of the lease.
3. Every deferral advances a store-wide trigger generation (PostgreSQL sequence `refresh_trigger_generation_seq`;
   SQLite one-row counter `refresh_trigger_generation_counter`). A cycle captures the generation before it reads its
   source; `resolve_after_publish` deletes the row only while the generation is unchanged. A trigger deferred during
   the cycle therefore survives the publish and fires again at once. Ordering never depends on wall clocks, which
   differ between nodes.

### Settling a refresh that did not publish

`skip_settler` binds `settle_skip` to the generation that existed before the refresh ran:

- An unserviceable skip (integrity or local-repair quarantine, orphaned clone, alias or registry entry gone) drops only
  that generation's trigger, so a newer one survives.
- A self-settled result ("Refresh complete", "No changes detected", "Refresh integrity gate failed; publish skipped")
  needs nothing.
- Any other skip (held write lock, uninitialised local repository, a failed local repair) keeps the trigger and
  escalates its retry interval by one failure (`escalate_refresh_trigger`), in one generation-conditional statement that
  keeps the original failure detail and never recreates a row a publish deleted.

## cidx-meta refresh requests

Writers to cidx-meta (repository descriptions, the memory store, the dependency map) request a refresh through
`request_cidx_meta_refresh` or the `CidxMetaRefreshDebouncer` (`src/code_indexer/global_repos/meta_description_hook.py`):

- A `RefreshDeferredError` needs no retry: the trigger is persisted and the scheduler fires it.
- A refresh already running (`DuplicateJobError`) is retried after the debounce interval.
- Any other failure keeps the refresh owed and retries at a doubling interval capped at 900 s
  (`_MAX_RETRY_INTERVAL_SECONDS`), never giving up.
- A submission settles only the signals that arrived before it started (`_signal_seq`); a write signalled while it was
  in flight stays owed. Each timer callback carries a token (`_timer_token`) and acts only while it is current, because
  a cancelled `threading.Timer` can still call back.

## Cancellation

A refresh submitted as a background job receives the job's `cancel_check` and passes it to every subprocess it starts
(git, `cidx init` repair, repository metrics, semantic, temporal and SCIP indexing, snapshot steps, cidx-meta backup).
The cycle re-checks cancellation before indexing, after indexing, after the integrity gate, before snapshot creation
and before the alias swap (`_raise_if_refresh_cancelled`). A snapshot created but not yet published when the cancel is
seen is handed to `CleanupManager.schedule_cleanup`, and the job ends `cancelled`. The CoW copy itself cannot be
interrupted midway; a cancel during it is honoured at the pre-swap check.

Refreshes executed for reclaimed cluster jobs (`execute_refresh_for_claimed_job`) receive no cancel check, so their
subprocesses do not stop on cancel.

## Storage

| Store | Schema |
|-------|--------|
| SQLite | `refresh_failure_backoff_state` and `refresh_trigger_generation_counter`, created by the SQLite metadata backend mixin (`server/storage/sqlite_backends/_refresh_failure_backoff_mixin.py`) |
| PostgreSQL | migrations `058_refresh_failure_backoff_state.sql`, `059_refresh_failure_backoff_pending_trigger.sql`, `060_refresh_failure_backoff_due_index.sql`, `061_refresh_failure_backoff_trigger_generation.sql` (mixin `server/storage/postgres/_refresh_failure_backoff_mixin.py`) |

Related invariants: [Indexing and Migrations](invariants/indexing-and-migrations.md),
[Chunk Storage](invariants/chunk-storage.md).
