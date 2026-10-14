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

## Activated copies

Same mechanism with a MUTABLE target (one path, re-published on every reindex):
`ensure_current_index` serves only the recorded version and swaps an older one;
`publish_index` publishes on top of the version the build started from. See
[content-seam-activated-repos.md](content-seam-activated-repos.md#the-copys-index-on-every-pod-index_store_backend-delegated).

## Leftover checkouts (`server/services/checkout_sweep.py`)

A copy deactivated, or a golden repo removed, on ANOTHER pod leaves this pod's
checkout behind. Every pod (pod-local state: not leader-gated) runs
`CheckoutSweeper` every `resource_config.index_checkout_sweep_interval_seconds`
(900) and removes, from targets carrying `.cidx-index-checked-out` only:

| Target | Removed when |
|---|---|
| `activated-repos/<user>/<alias>` (and a composite's marked members) | the registry has no `(user, alias)`, or its entry names another IndexStore alias |
| `golden-repos/.versioned/<repo>/v_*`, `golden-repos/<repo>` | no alias pointer exists for `<repo>` and none names the path (superseded snapshots of a live repo are pruned at checkout) |

Only checkout state goes (index root, markers, swap dirs); the directory only
once empty. Kept: unmarked targets (built here, never checked out or
published), anything whose marker changed within
`index_checkout_sweep_grace_seconds` (900; re-checked under the target's
checkout lock), anything a query on this pod holds (marked quiescing, skipped
while its refcount is non-zero). A registry or pointer read failure removes
nothing (ERROR, reported). Local store: no sweeper.

## A target re-published in place: HNSW orphan repairs

`.versioned` snapshots are never swept (S2 rebuilds them clean). A clone path
the alias no longer serves is this pod's build source: repaired in place, and
the repair reaches every pod with the next refresh's publish -- as locally.
But a clone-path TARGET (a repo added, not yet refreshed) is a published
version every pod checks out, so the sweep
(`HNSWOrphanRepairSweepScheduler._process_published_golden`) makes this pod
hold the recorded version, repairs it, publishes on top of exactly that version
(store CAS) and records the new version for the same target only if the alias
still names it with the starting version (`AliasManager.record_republished_index`).
Every checkout -- and every publisher's own copy (refresh, add, branch change,
migration snapshot: `index_materializer.mark_published_here`) -- carries
`.cidx-index-checked-out` with its version; a pod whose marker names another
version than the alias records swaps its copy (rename, with rollback). An
unmarked index (built before markers) is served as is. `golden_temporal`
shards are pod-local (never published) and stay in-place repairs. Pruning
removes only marked snapshots OLDER than the target (a newer one may be a
refresh about to swap).

## Not covered

* A write-mode redirect (live source path) is served as before.
* `local://` repositories other than cidx-meta are pod-local by design and are
  not published at add; cidx-meta is published by its refresh.
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
`tests/unit/server/services/test_unpublished_swaps_fail_loud.py`.
