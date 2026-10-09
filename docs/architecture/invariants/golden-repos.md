# Golden Repository and Versioned Snapshot Invariants

Rules for golden repositories, alias pointers, immutable snapshots, activation and the registry. The lifecycle these
rules protect is described in [Repository Lifecycle](../repository-lifecycle.md). Index of all invariant groups:
[README](README.md).

## Mutable base clone versus immutable snapshot

- The base clone `{golden_repos_dir}/{alias}/` is mutable: fetches, pulls and indexing happen there.
- A versioned snapshot `{golden_repos_dir}/.versioned/{alias}/v_{timestamp}/` is immutable once created. Never
  modify, check out or index inside `.versioned/`.
- `GoldenRepoManager.get_actual_repo_path(alias)` (`src/code_indexer/server/repositories/golden_repo_manager.py`)
  returns the mutable base clone whenever it exists, and falls back to the newest `.versioned/{alias}/v_*` only when it
  does not. It cannot be used to prove that a path is immutable.
- For global repositories the alias JSON `target_path` is authoritative; after the first refresh it points at a
  snapshot.
- A path-keyed cache must prove immutability with `is_immutable_versioned_snapshot(path)`
  (`src/code_indexer/server/services/query_path_cache.py`) and use a short TTL for anything it does not prove.

## Canonical snapshot predicate

- `is_versioned_snapshot(path, *, mount_point=None)` in `src/code_indexer/server/storage/shared/snapshot_paths.py` is
  the only authority. Canonical shape: a `/.versioned/` segment, a leaf matching `v_<digits>`, and the namespace
  directory as immediate parent. Legacy cow-daemon (`{mount}/{ns}/v_<ts>`) and flat ONTAP (`{mount}/v_<ts>`) shapes are
  recognised only when `mount_point` is supplied, and are never created. `{mount}/activated-repos/...` and the base
  clone always test false.
- Callers use the facade `VersionedSnapshotManager.is_versioned_snapshot(path)`
  (`src/code_indexer/server/storage/shared/snapshot_manager.py`), which supplies the backend mount. Never reimplement a
  `".versioned" in path` substring test.
- Snapshot discovery goes through `VersionedSnapshotManager.list_snapshots(alias)` and `latest_snapshot(alias)`,
  never a re-glob of `golden_repos_dir/.versioned`. On the ONTAP backend discovery returns an empty list, which
  disables retention there.
- `CowDaemonBackend` creates snapshots at `{mount}/.versioned/{ns}/{name}` and applies `_sanitize_identifier`
  uniformly on create, delete, list and exists.

## Publication, cleanup and retention

- Every alias swap site (refresh scheduler, `_cb_swap_alias`, add-index) schedules the old target for cleanup only
  when the facade recognises it as a snapshot and it is not the base clone.
- Deletion runs only through `CleanupManager` (`src/code_indexer/global_repos/cleanup_manager.py`): it requires the
  QueryTracker reference count for the path to be zero and at least `MIN_RETENTION_AGE_SECONDS` (900 s by default)
  since scheduling. Snapshot paths are deleted through the snapshot manager so each backend frees them correctly.
  Never delete a snapshot directly at a swap site: queries may still be reading it.
- After each successful swap `RefreshScheduler._enforce_retention` keeps the newest
  `snapshot_retention_keep_last` snapshots (runtime setting, default 3; a value below 1 or an unreadable setting falls
  back to 3) and never schedules the current `target_path` or the `previous_path` (`AliasManager.get_previous_path`).
- Alias pointer writes (`create_alias`, `swap_alias` in `src/code_indexer/global_repos/alias_manager.py`) write a
  temporary file, `os.replace` it and fsync the aliases directory.
- Snapshot version ids are collision-checked: a computed `v_{timestamp}` that already exists is retried with an
  incremented timestamp, bounded by `_MAX_VERSION_ID_COLLISION_RETRIES = 100`.

## Registry-orphan guard

A `golden_repos` row must never exist without its on-disk clone and alias pointer.

- Registration is all-or-nothing. If global activation (alias pointer and global registry entry) fails, the row is
  removed and the fresh clone deleted before the error propagates.
- Removal (`remove_golden_repo`) runs in this order: delete the user activations of the repository, delete the
  registry row, delete the versioned snapshots (`_cleanup_versioned_snapshots`), deactivate the global alias and run
  the other detach steps (description, group access, wiki records), then delete the clone. The detach steps after the
  row removal are individually non-fatal and run unconditionally, never behind a "file cleanup succeeded" check (the
  final clone deletion quarantines what it cannot remove and then raises, and a refresh in progress or a held write
  lock refuses the removal before any file is touched); any new
  teardown step belongs in that unconditional block. A failure after the row is gone can leave only orphan files,
  never an orphan row.
- Snapshots are deleted before the `-global` pointer: a non-local clone backend may need the pointer to identify the
  on-disk namespace, and the snapshot sweep skips any namespace whose pointer is gone, so deleting the pointer first
  could strand the snapshots.
- `reconcile_golden_repo_registry` (`src/code_indexer/server/services/golden_repo_reconciler.py`) runs once at
  startup, single-flighted across workers and nodes with `register_job_if_no_conflict`:
  - It first checks that `golden_repos_dir` is a readable directory (`_golden_repos_dir_is_healthy`); otherwise it
    does nothing.
  - It classifies every registered alias with `get_actual_repo_path` before deleting anything, and refuses to remove
    anything when more than `ORPHAN_FRACTION_ABORT_THRESHOLD` (0.5) of the fleet resolves absent.
  - A refused set is removed only after the same fingerprint of aliases is seen on
    `CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD` (3) consecutive sweeps, each at least
    `MIN_BREAKER_OBSERVATION_GAP_SECONDS` (30 min) apart, with a healthy directory each time. State lives in
    `golden_repo_reconcile_breaker_state` (SQLite or PostgreSQL). On a confirmed sweep the confirmation count is
    reset only after the removal pass actually removed at least one alias (a normal sweep or an unhealthy-directory
    event also resets it), so a confirmed sweep whose removals all fail keeps its count. A
    tripped breaker is reported as DEGRADED on `/health`; the last automatic removal is reported as
    `last_golden_repo_reconcile_auto_heal`.
  - A healthy, globally active repository missing its `-global` pointer gets the pointer rewritten, never deleted.
  - Global registry entries whose `golden_repos` row is gone are deactivated (`_reconcile_global_registry_orphans`),
    each re-confirmed with an authoritative read; a zero-row registry read suppresses this pass, and aliases the
    first pass saw with a row are skipped so it never races removals already in flight.
- `reconcile_versioned_snapshots` reclaims a `.versioned/{alias}` namespace whose pointer is missing only when the
  alias is also absent from the registry and its base clone is absent by an explicit `os.stat()`.

## Reads of mutable registry fields

`_resolve_golden_repo` serves cached rows (reload on miss). Decisions that depend on a field another node can change
(`default_branch`, `temporal_options`) read through `_resolve_golden_repo_authoritative`, which bypasses the cache.

## Temporal flags and options

- `golden_repos_metadata` is the authoritative store of a golden repository's `temporal_options` (`max_commits`,
  `since_date`, `diff_context`, `all_branches`). `global_repos.temporal_options` is written at registration only and
  must not be read as current.
- `enable_temporal` reconciliation (`RefreshScheduler`) is one-way per table: a stored `True` is downgraded to `False`
  when no real temporal data exists on disk; a stored `False` is never turned back on automatically.
  `enable_scip` reconciliation is separate and bidirectional.

## Activation wiring

- `ActivatedRepoManager._clone_with_copy_on_write` clones through `self._clone_backend.create_clone_at_path(...)` and
  raises when no backend is set. The backend is assigned after construction in
  `src/code_indexer/server/startup/lifespan.py` (`arm._clone_backend = snapshot_manager._clone_backend`); keep that
  assignment (guard: `tests/unit/server/startup/test_lifespan_clone_backend_wiring_bug1044.py`).
- Activation, branch switch and sync on a non-default branch run `ActivatedRepoManager._run_branch_delta_index`
  (semantic only), skipped for the default branch, `-global` aliases, or when `_index_manager` is `None`.
  `_index_manager` is also assigned in `lifespan.py`; removing that assignment silently disables the reindex
  (guard: `tests/unit/server/startup/test_lifespan_index_manager_wiring_bug1203.py`). A failed reindex raises
  `ActivatedRepoError`; on success the HNSW and id-index caches are invalidated by prefix
  (`{repo}/.code-indexer/index`), because they are keyed per collection.
- `ActivatedRepoIndexManager.trigger_reindex` rejects `temporal` for activated repositories: temporal data belongs
  to the golden repository.
