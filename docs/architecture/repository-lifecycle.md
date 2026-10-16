# Repository Lifecycle

How CIDX Server holds repositories: golden repositories and their mutable base clones, alias pointers, immutable
versioned snapshots, global repositories, and per-user activated repositories. Audience: maintainers and
contributors. Rules this lifecycle must keep: [Golden Repository Invariants](invariants/golden-repos.md). What happens
when a refresh fails: [Refresh Recovery](refresh-recovery.md). Index storage inside each repository:
[Storage](storage.md).

## Roles

| Role | What it is | Path | Mutable |
|------|------------|------|---------|
| Golden repository | an admin-registered source repository; its base clone is where fetches and indexing happen | `{golden_repos_dir}/{alias}/` | yes |
| Versioned snapshot | a copy-on-write copy of the indexed base clone, published to queries | `{golden_repos_dir}/.versioned/{alias}/v_{timestamp}/` | no |
| Global repository | the shared, read-only view of a golden repository, queried as `{alias}-global` | the alias pointer's `target_path` (a snapshot after the first refresh) | no |
| Activated repository | a user's own CoW clone, for branch switching and write tools | `{data_dir}/activated-repos/{username}/{user_alias}/` | yes |

`golden_repos_dir` is `~/.cidx-server/data/golden-repos`. In a cow-daemon cluster it and `activated-repos` are
symlinks into the shared CoW mount ([CoW Storage Setup](../server/cow-storage-setup.md#storage-layout)).

```
{golden_repos_dir}/
  {alias}/                       base clone + .code-indexer/ (mutable)
  .versioned/{alias}/v_<ts>/     published snapshots (immutable)
  aliases/{alias}-global.json    alias pointers
  .temporal/{alias}/             server temporal index (see storage.md)
  cidx-meta/                     descriptions, dependency map, memories, X-Ray patterns
```

## Registries

- Golden repository metadata: table `golden_repos_metadata` (SQLite in solo mode, PostgreSQL in cluster mode),
  accessed through `GoldenRepoManager` (`src/code_indexer/server/repositories/golden_repo_manager.py`). It is the
  authoritative store of `temporal_options`.
- Global repositories: table `global_repos`, written by `GlobalActivator`
  (`src/code_indexer/global_repos/global_activation.py`).
- Alias pointers: JSON files in `{golden_repos_dir}/aliases/`, managed by `AliasManager`
  (`src/code_indexer/global_repos/alias_manager.py`). Fields include `target_path`, `previous_path`, `swapped_at` and
  `last_refresh`. Every write is a temporary file plus `os.replace`; the publication writes (`create_alias`,
  `swap_alias`) also fsync the aliases directory, `update_refresh_timestamp` does not.
- Activated repositories: one `{user_alias}_metadata.json` per activation in solo mode; table `activated_repos` in
  cluster mode.

## Registration

1. Clone into `{golden_repos_dir}/{alias}/` and index the base clone (semantic and FTS, plus temporal and SCIP when
   requested).
2. Global activation: create `aliases/{alias}-global.json` pointing at the base clone and register `{alias}-global` in
   `global_repos`.
3. All or nothing: if activation fails, the registry row and the fresh clone are removed and the registration fails.

## Refresh and publication

`RefreshScheduler` (`src/code_indexer/global_repos/refresh_scheduler.py`) refreshes each global repository on its
interval or on request:

1. Fetch and update the mutable base clone (local repositories such as cidx-meta are indexed from their live
   directory instead).
2. Index the base clone in place (`_index_source`, a `cidx index` child).
3. Run the integrity gate on every `chunks.db`.
4. Create a snapshot: `cp --reflink=auto -a` of the base clone into `.versioned/{alias}/v_{timestamp}/` through the
   configured clone backend (`_create_snapshot`), which inherits the indexes without re-indexing.
5. Swap the alias pointer to the new snapshot (`swap_alias`, which records the old target as `previous_path`).
6. Schedule the old snapshot for cleanup and enforce retention.

A cycle that stops before step 5 publishes nothing; the previous snapshot keeps serving. Because the alias pointer is
authoritative for global repositories, queries against `{alias}-global` read immutable snapshots after the first
refresh. `GoldenRepoManager.get_actual_repo_path(alias)` returns the mutable base clone whenever it exists, so it is
the right resolver for git and indexing work on the golden repository and the wrong one for proving immutability.

## Snapshot identity, retention and cleanup

- A path is a snapshot only if the canonical predicate `is_versioned_snapshot()`
  (`src/code_indexer/server/storage/shared/snapshot_paths.py`) says so; callers use the
  `VersionedSnapshotManager.is_versioned_snapshot()` facade, which knows the backend mount.
- Retention keeps the newest `snapshot_retention_keep_last` snapshots (runtime setting, default 3) and never the
  current or previous target.
- `CleanupManager` deletes a snapshot only when no query holds a reference to it (QueryTracker reference count zero)
  and it has been scheduled for at least 900 s, through the snapshot manager so each clone backend frees it correctly.

## Activation

- `ActivatedRepoManager` (`src/code_indexer/server/repositories/activated_repo_manager.py`) creates the user's copy
  with the clone backend's `create_clone_at_path` (`local`: `cp --reflink=auto`; `cow-daemon`: daemon REST API on the
  shared filesystem; `ontap`: FlexClone), from the golden repository.
- The clone gets two remotes: `origin` (the upstream URL) and `golden` (the golden base clone, used for sync).
- Each activation gets a unique `activation_id`, which is part of the server's index cache keys so a deactivate and
  reactivate at the same path never reuses a cached index.
- Activating, switching branch or syncing on a non-default branch runs a semantic delta reindex of the clone. Temporal
  indexes are never built for activations; their temporal queries read the golden repository's temporal index.

## Deactivation and removal

- Deactivation waits a bounded time for in-flight queries on the repository to finish, then moves the clone aside and
  deletes it.
- Removing a golden repository (`remove_golden_repo` in `golden_repo_manager.py`) runs in this order: delete every
  user activation of it, delete the registry row, delete its versioned snapshots (`_cleanup_versioned_snapshots`,
  deliberately before the `-global` pointer goes), deactivate the global alias and detach the description, group
  access and wiki records, then delete the clone.
- At startup `reconcile_golden_repo_registry` repairs drift between the registry and disk: rows whose clone is gone
  are removed (refusing mass deletions that look like a mount failure), missing `-global` pointers are rewritten, and
  global entries without a row are deactivated. Details: [Golden Repository Invariants](invariants/golden-repos.md#registry-orphan-guard).

## Path validation

Aliases containing `..`, `/` or `\` are rejected, and `get_actual_repo_path` refuses a resolved path (after
`os.path.realpath`) outside `golden_repos_dir`.
