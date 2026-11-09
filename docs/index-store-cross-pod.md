# IndexStore: serving a published index on every pod

With `index_store_backend: delegated` a pod builds an index in its own scratch,
**publishes** it to the artifact service (workspace-mcp `/index/publish`) and
checks the published version out where readers open it. Alias pointers are
control-plane metadata shared by every pod; the checked-out directories are not.
This page is how a pod that did NOT publish serves the index.
With `index_store_backend: local` (the default) nothing here runs.

## What a publisher records

| Publisher | Pointer target | Recorded with the pointer (same write) |
|---|---|---|
| Golden add (`GoldenRepoManager._publish_added_index`) | clone path `golden-repos/<alias>` | `index_store_alias`, `index_version` |
| Refresh / add-index / provider-index swap (`RefreshScheduler.published_swap_extra`) | `.versioned/<alias>/v_<ts>` | `index_store_alias`, `index_version` (+ cidx-meta fingerprint) |
| Branch change (`GoldenRepoManager.change_branch`, after its FTS/HNSW cleanup) | `.versioned/<alias>/v_<ts>` | `index_store_alias`, `index_version` |
| Fleet-migration snapshot (`fleet_migration/snapshot_trigger.py`) | `.versioned/<alias>/v_<ts>` | `index_store_alias`, `index_version` |

The sites outside the refresh publish through `utils.index_store.publish_index_fields`.
A delegated swap to a snapshot with no publication, or to a path other than the
one this refresh published, is refused. A failed publish fails the add (the
registration rolls back), the refresh, the branch change or the migration step
before the pointer moves. `AliasManager.swap_alias` without those two fields
DROPS the old ones (they describe the old target), so any swap that did not
publish fails loud on other pods instead of serving the old version.

Delegated requires `content_backend` and `ripgrep_backend` to be `delegated`
too (startup refuses otherwise): a snapshot -- the publisher's and a checkout
alike -- holds index state only, and file, regex and X-Ray reads of it go
through those seams to the base clone (`golden_repo_content_root`).

## What a reader does (`server/services/index_materializer.py`)

`AliasManager.ensure_served_here(alias, target)` runs after the alias is
resolved by every golden read path (`resolve_alias_or_index_path`,
`_resolve_golden_repo_path`, `SemanticQueryManager._perform_search`):

1. Local store, or `<target>/.code-indexer/index` present with no in-progress
   marker: nothing to do (the publisher, or a pod that checked it out before).
2. Otherwise check the RECORDED version out to `<target>/.code-indexer`, run
   `cidx fix-config --force` in `<target>` (as the publisher does), verify the
   index directory, mark the target `.cidx-index-checked-out`.
3. One checkout per target (thread lock + `flock` on `.<target>.checkout.lock`).
   A crash leaves `.cidx-index-checkout-in-progress`; the next reader redoes it.
   A `.code-indexer` without index and without that marker is refused, never
   replaced.
4. Prune: this pod's other checked-out `v_*` of the same repository that are
   neither the alias target nor its previous path are handed to the
   `CleanupManager` (deleted once no query or reader lease holds them, after
   the minimum retention age). Snapshots this pod built itself are left to
   normal retention.

Fail loud: no recorded version (a pointer written before this existed -- refresh
the repository), an unreachable store, a failed checkout or fix-config raise
`PublishedIndexUnavailableError`; the query errors, never returns empty.

Readers that are not queries do the same (`alias_manager.served_golden_target`):
golden index status (`get_golden_repo_indexes`, `_index_exists`, MCP
`get_index_status` on a `-global` alias) and the refresh's START registry
reconcile, which is skipped -- never an auto-disable -- when this pod holds
nothing and nothing is recorded. The `cidx.repos.indexed` gauge reads only the
recorded version (no checkout). An activation checks the golden alias's
recorded version out straight into the copy's index root, without serving it
under golden-repos. With every repository backend delegated too, nothing cidx
needs is on the golden-repos volume: [content-seam-golden-mount.md](content-seam-golden-mount.md).

## Temporal shards (`server/services/temporal_index_store.py`)

A golden repo's temporal shards live at one fixed, mutable root,
`{golden_repos_dir}/.temporal/{alias}/`, outside any snapshot. Under a delegated
store they are their own artifact, `temporal.<sha256(repo)>` (golden index
aliases are kept out of that namespace), recorded with the pointer as
`temporal_index_store_alias` / `temporal_index_version`:

| Step | What |
|---|---|
| Refresh / add-index temporal child | serve the recorded shards first (an incremental pass builds on the latest), run, then publish them with the job's snapshot, in the same pointer write; a job that did not rebuild them leaves the recorded version alone |
| Readers (temporal search, multi-repo temporal, live temporal worker, temporal status, repository health) | `served_temporal_index_root`: hold the recorded version -- checked out, or an older one swapped by rename with rollback; markers beside the root (`.<alias>.temporal-checked-out`, `.<alias>.temporal-in-progress`) |
| HNSW orphan sweep (`golden_temporal`) | serve the recorded version, repair, publish on top of it (CAS), record via `update_fields_if` (best-effort, not atomic; a stale write is corrected by the next refresh) |

A pointer recording no temporal version (not enabled, or written before this)
is served from what the pod holds, as before; a store failure is an error.

## Leftover sweep and served-index cache (disk)

A checkout lives on the pod's own disk (20 GiB in EKS, ~180 MB per repository,
~900 repositories), so two per-pod loops keep it bounded. Both run on every
pod, under `delegated` only, and share one scanner and one removal
(`served_index_cache.iter_*_checkouts`, `retire_pod_state`: the index root is
renamed aside under the target's checkout lock, then deleted).

| Loop | Removes | Module |
|---|---|---|
| Leftover sweep (every 15 min) | checkouts whose copy or golden alias is gone | `server/services/checkout_sweep.py` |
| Served-index cache (every 60 s, and before each checkout) | least recently served checkouts, from the high watermark down to the low one | `server/services/served_index_cache.py` |

Served-index cache:

* **Scope**: directories carrying `.cidx-index-checked-out` (temporal:
  `.<repo>.temporal-checked-out`) -- golden clone paths and `.versioned/*/v_*`,
  activated copies (a composite and its members as ONE entry), temporal shard
  roots. An unmarked directory (an unpublished build) is never a candidate.
* **LRU**: access recorded in memory on every serve; at most every 5 min (and
  at most a third of `min_idle_seconds`) also as the mtime of
  `.cidx-index-last-served` (temporal: `.<repo>.temporal-last-served`), so the
  order survives a restart and is shared by the pod's processes. Never served
  since written: the marker's mtime.
* **Reads hold what they serve until they end** (`reads_served_indexes` /
  `served_index_reads`): a checkout served inside a read scope -- a hit, or a
  checkout made for it -- is held by its readers lock SHARED until the read
  returns, in whichever process runs it. Scoped: REST `/api/query`,
  `SemanticQueryManager._perform_search` (MCP search, query jobs), MCP global and
  temporal search, live temporal dispatch and the live temporal worker,
  multi-repo semantic / FTS / temporal / regex, repository indexes status and
  the health check job, diagnostics HNSW validation (which checks only the
  indexes this pod holds -- `hold_if_present`, no checkout, no LRU access --
  and lists the others as `repos_not_on_this_pod`, not as unhealthy),
  activated index status
  (manager and route), temporal status (`get_temporal_repo_status`,
  `get_temporal_repo_max_commits`), every `SCIPQueryService` query and every
  multi-repo SCIP worker, `SemanticSearchService.search_repository`, and the web
  UI query routes. A background job inherits nothing from the request that
  queued it, so the temporal worker and the health check job serve again inside
  their own scope. Work that outlives its read -- the SCIP context watchdog
  thread after a timeout, the fire-and-forget fidelity audit -- carries its own
  copy of the read's holds (`carry_served_index_reads`: `os.dup` of each locked
  descriptor, released when the work ends, or at once when it never runs).
* **No all-repository SCIP query**: under `delegated`, a `SCIPQueryService`
  query without `repository_alias` raises `UnscopedScipQueryError` instead of
  resolving -- and so checking out -- every golden repository.
* **More than one process** (the EKS StatefulSet runs `--workers 2`): every
  worker runs its own cache over the same disk. A query a `QueryTracker` counts
  holds `.<target>.readers.lock` SHARED from this process's first ref to its
  last; a serve holds it SHARED while it records the access. An eviction holds
  it EXCLUSIVE (never waiting) from before it reads the access time until the
  index is renamed away: a query in ANY process keeps the checkout, a serve in
  any process during an eviction is turned away and checks out again. A serve
  in another process keeps it for at least two thirds of `min_idle_seconds`
  (the stamp).
* **Never evicted** (skipped, counted under `skips`): pinned (`pinned_served_index`:
  activation from index seed to publish, activated reindex, branch delta, HNSW
  orphan repair -- a pin also holds the readers lock SHARED for its whole run,
  so an eviction in any process skips it, and taking a pin waits out an
  eviction already running), another purge marking it quiescing (a
  deactivation or the leftover sweep: an eviction, and the sweep, clear only a
  mark they set -- `QueryTracker.try_mark_quiescing`), the golden repo's write
  lock held (refresh builds the base index and temporal shards in place), a
  query holding it (`QueryTracker`; marked quiescing while evicted), a live
  snapshot reader lease, served within `min_idle_seconds`, its checkout lock
  busy (only ever TRIED -- eviction cannot deadlock a checkout making room).
  A query that takes a plain `increment_ref` (MCP search) is covered by the
  access its alias resolution just recorded, so `min_idle_seconds` must be > 0.
* **A failing victim** (`OSError` while retiring it): logged, counted as
  `error`, whatever it already renamed aside is deleted, and the pass -- or the
  admission of an unrelated checkout -- goes on with the next one.
* **Race**: a reader whose fast "already served" check lands during an eviction,
  or just before one that finishes first, is told so (`note_hit` decides under
  the cache lock: marked as evicting, or the index root is gone), waits for the
  lock and checks out again;
  an open file keeps its data. The next serve after an eviction checks it out
  again transparently (and closes this process's cached HNSW/FTS handles).
* **Admission**: before a checkout, room for its expected size (its last known
  size, else the mean) is made. One checkout larger than the cache may hold (the
  byte watermark, or the disk less the free floor) is removed and refused with
  `ServedIndexTooLargeError` (a `PublishedIndexUnavailableError`); that version
  is not downloaded again.
* **Observability**: `Served-index cache: evicted <alias> (<path>) <n> bytes,
  reason=watermark|admission, idle <s>s` (INFO); `served_index_cache` in
  `GET /health` and `GET /cache/stats` (entries, bytes, disk, watermarks, hits,
  misses, evictions, evicted bytes, refusals, skips). Absent under `local`.

| `resource_config` key | Default | Meaning |
|---|---|---|
| `served_index_cache_max_bytes` | 14 GiB | high watermark: cached checkouts' total bytes (0: off) |
| `served_index_cache_low_bytes` | 12 GiB | evict down to this |
| `served_index_cache_min_free_percent` | 15 | high watermark: free disk below this % (0: off) |
| `served_index_cache_target_free_percent` | 25 | evict until this % is free |
| `served_index_cache_min_idle_seconds` | 900 | never evict one served more recently (> 0) |
| `served_index_cache_interval_seconds` | 60 | background pass interval (> 0) |

Invalid watermarks (low above high, target free below min free) and a
non-positive pass interval fail startup. The leftover sweep refuses a
non-positive `index_checkout_sweep_interval_seconds` too, and skips a pinned
checkout or one whose readers lock any process holds.
The publisher's own `golden-repos/<repo>` index (the refresh's incremental base)
is a marked checkout too: evicted only when no refresh holds the write lock. No
query serves it, so each refresh records it as used (`note_used`, not a hit): it
ages from its last refresh, not its first publish. An evicted base never costs a
re-embed: before indexing, the refresh checks the latest published version out
there (`RefreshScheduler._seed_incremental_base`) and `cidx index` runs
incrementally on top of it -- also on a pod that never held the base. Only a
repository with nothing published yet is built from scratch; a failed checkout
fails the refresh rather than rebuilding silently.

## Not covered

* A write-mode redirect (live source path) is served as before.
* `local://` repositories other than cidx-meta are pod-local by design and are
  not published at add; cidx-meta is published by its refresh, which first
  checks the latest version out on a pod that does not hold it (or runs
  `cidx init` there when nothing is published yet).
* The FTS and semantic backfill sweeps, the GAP-4 staleness pass and the
  startup `.versioned` orphan sweep do not run: they judge or build indexes in
  place on one pod.
* The reconciler's missing-pointer repair (`golden_repo_reconciler.py`) writes
  a pointer with no recorded version: other pods fail loud until the next
  refresh publishes.
* The refresh integrity gate reads the CURRENT target's point counts on the
  refreshing pod; a pod that never checked that target out compares against
  nothing (as on a first refresh).

Tests: `tests/unit/server/services/test_index_materializer_cross_pod.py`,
`tests/unit/server/repositories/test_golden_add_publishes_index.py`,
`tests/unit/server/repositories/test_add_index_swap_records_published_index.py`,
`tests/unit/server/mcp/test_swap_sites_record_published_index.py`,
`tests/unit/server/services/test_unpublished_swaps_fail_loud.py`,
`tests/unit/server/services/test_checkout_sweep.py`,
`tests/unit/server/services/test_served_index_cache.py`,
`tests/unit/global_repos/test_refresh_base_seeded_after_eviction.py`,
`tests/unit/server/services/test_served_index_read_paths.py`,
`tests/unit/server/test_golden_mount_removal.py`.
