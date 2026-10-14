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

A delegated swap to a snapshot with no publication, or to a path other than the
one this refresh published, is refused. A failed publish fails the add (the
registration rolls back) or the refresh.

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
   neither the alias target nor its previous path are removed. Snapshots this
   pod built itself are left to normal retention.

Fail loud: no recorded version (a pointer written before this existed -- refresh
the repository), an unreachable store, a failed checkout or fix-config raise
`PublishedIndexUnavailableError`; the query errors, never returns empty.

## Activated copies

Same mechanism with a MUTABLE target (one path, re-published on every reindex):
`ensure_current_index` serves only the recorded version and swaps an older one;
`publish_index` publishes on top of the version the build started from. See
[content-seam-activated-repos.md](content-seam-activated-repos.md#the-copys-index-on-every-pod-index_store_backend-delegated).

## After rollout: backfill what predates this (one-off)

Pointers and activated registrations written before cross-pod serving carry no
recorded version; other pods fail loud on them. Run the backfill
(`server/services/index_store_backfill.py`), admin + TOTP elevation:

```bash
# 1. plan (default dry_run=true): what each pod would do
curl -sX POST "$CIDX/api/admin/index-store/backfill" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
# 2. on EVERY pod (golden refreshes dedup cluster-wide; each pod publishes the
#    activated copies whose index it holds)
for pod in $(kubectl -n claude-code-indexer get pods -l app=cidx -o name); do
  kubectl -n claude-code-indexer exec "$pod" -- curl -sX POST \
    localhost:8000/api/admin/index-store/backfill -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d '{"dry_run": false}'
done
# 3. copies still in activated_not_held_here everywhere (their pod is gone):
#    rebuild them from the golden index + a clear reindex (embedding cost)
curl -sX POST "$CIDX/api/admin/index-store/backfill" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": false, "rebuild_missing": true}'
```

| Item | Action | Report field |
|---|---|---|
| golden pointer without a version (git URL) | forced refresh (`force_reset`): re-index, publish, record | `golden_refreshed` (job id) |
| golden `local://`, unregistered alias | none | `golden_skipped` (reason) |
| activated copy held on this pod | publish as is (over any leftover), record | `activated_published` |
| activated copy held nowhere, `rebuild_missing` | seed from the served golden index, clear reindex (job publishes) | `activated_rebuilt` (job id) |
| otherwise | none -- run on its pod or rebuild | `activated_not_held_here` |
| any failure | none | `golden_failed` / `activated_failed` (cause) |

Idempotent (recorded items are skipped). Local IndexStore: 409. A registry read
failure: 500, nothing done. Composite copies are never rebuilt (re-activate).

## Not covered

* A write-mode redirect (live source path) is served as before.
* `local://` repositories other than cidx-meta are pod-local by design and are
  not published at add; cidx-meta is published by its refresh.
* The refresh integrity gate reads the CURRENT target's point counts on the
  refreshing pod; a pod that never checked that target out compares against
  nothing (as on a first refresh).

Tests: `tests/unit/server/services/test_index_materializer_cross_pod.py`,
`tests/unit/server/repositories/test_golden_add_publishes_index.py`,
`tests/unit/server/repositories/test_add_index_swap_records_published_index.py`,
`tests/unit/server/mcp/test_swap_sites_record_published_index.py`.
