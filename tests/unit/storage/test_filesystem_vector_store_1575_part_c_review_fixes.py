"""TDD tests for the post-implementation review fixes to Bug #1575 Part C.

Two independent reviewers (Claude opus and Codex) traced the real code and
found matching defects, re-verified by the orchestrator against the actual
files before this test module was written. Each test below reproduces one
defect using a REAL FilesystemVectorStore + real HNSW + real filesystem
(no mocking of the code under test), following the same conventions as
test_filesystem_vector_store_1575_part_c_locking_and_crash.py.

Currently covers:
- Finding 1: a watch-mode (skip_hnsw_rebuild=True) end_indexing() call must
  discard its in-memory HNSWSyncSession so the NEXT cycle's session is not
  a stale alias of the previous cycle's now-defunct tracking sets.
- Defect 2 (abort/resume sequence): a session created AFTER abort_indexing()
  discarded a prior, incomplete session must never be trusted as "complete"
  for incremental publish when its start_epoch does not match the currently
  published epoch -- otherwise the earlier aborted session's already-durable
  mutations are silently never applied to the HNSW graph while the state is
  published as clean.
- Defect 2 (two-instance interleaved sequence): two separate
  FilesystemVectorStore instances against the SAME on-disk collection, with
  their begin_indexing()/upsert_points() calls sequentially interleaved,
  must never let the first instance's end_indexing() publish "clean" over
  the second instance's mutation that its own session never tracked.
"""

import json

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


class _UnusedEmbeddingProvider:
    """Placeholder passed as `embedding_provider` -- never invoked because
    every search() call below supplies `precomputed_query_vector`."""


def _vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _point(point_id, path, seed):
    return {
        "id": point_id,
        "vector": _vector(seed),
        "payload": {"path": path, "type": "content", "hidden_branches": []},
    }


def _hnsw_sync(collection_path):
    """Bug #1619: resolve hnsw_sync the same way production code does --
    prefer the dedicated hnsw_sync_state.json file, falling back to the
    legacy embedded collection_meta.json key for pre-migration collections."""
    sync_file = collection_path / "hnsw_sync_state.json"
    if sync_file.exists():
        return json.loads(sync_file.read_text())
    meta_file = collection_path / "collection_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    return meta.get("hnsw_sync")


def _hnsw_id_mapping(collection_path):
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    return set(meta["hnsw_index"]["id_mapping"].values())


def _query_ids(store, collection_name, seed, limit=50):
    results = store.search(
        query="unused",
        embedding_provider=_UnusedEmbeddingProvider(),
        collection_name=collection_name,
        limit=limit,
        precomputed_query_vector=_vector(seed),
    )
    return {r["id"] for r in results}


# ---------------------------------------------------------------------------
# Finding 1: stale aliased session survives a watch-mode end_indexing()
# ---------------------------------------------------------------------------


def test_finding1_watch_mode_end_indexing_discards_session_so_next_cycle_is_fresh(
    tmp_path,
):
    """Baseline: a real prior clean full build (point 'seed'), so a later
    incremental-update attempt has a valid graph to apply against instead
    of failing and falling back to a full rebuild (which would scan real
    disk state and mask this exact bug).

    Cycle 1 (watch mode) upserts point 'a' and calls
    end_indexing(skip_hnsw_rebuild=True). Cycle 2 (normal) upserts a
    DIFFERENT point 'b' and calls end_indexing(skip_hnsw_rebuild=False).

    Pre-fix: cycle 1's HNSWSyncSession is never discarded on the watch-mode
    branch, so cycle 2's _get_or_create_hnsw_sync_session() call returns
    THAT stale session object (created in cycle 1, aliased to cycle 1's now
    deleted _indexing_session_changes sets) instead of a fresh one aliased
    to cycle 2's real tracking sets. Cycle 2's end_indexing() then trusts
    the stale (frozen, 'a'-only) added/updated/deleted sets as "complete",
    performs an incremental update that applies 'a' but never 'b' (a
    genuinely valid incremental application, so it succeeds rather than
    falling back), and publishes the epoch as clean -- 'b' is on disk but
    never enters the HNSW graph.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"

    # Baseline: a real prior clean full build.
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("seed", "src/seed.py", 0)])
    _ = store.end_indexing("coll")
    baseline_sync = _hnsw_sync(collection_path)
    assert baseline_sync["status"] == "clean"

    # Cycle 1: watch mode.
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    _ = store.end_indexing("coll", skip_hnsw_rebuild=True)

    sync_after_watch = _hnsw_sync(collection_path)
    assert sync_after_watch["status"] == "dirty", (
        "watch mode must leave hnsw_sync dirty -- rebuild deferred to query time"
    )

    # Cycle 2: normal (non-watch) refresh, a genuinely new mutation.
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("b", "src/b.py", 2)])
    result = store.end_indexing("coll", skip_hnsw_rebuild=False)

    assert result["status"] == "ok"
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["status"] == "clean", (
        "cycle 2 must publish a clean epoch after a real rebuild/update"
    )

    id_mapping_values = _hnsw_id_mapping(collection_path)
    assert "b" in id_mapping_values, (
        "point 'b' (added in cycle 2) must be present in the HNSW artifact "
        "-- pre-fix, cycle 2 trusted cycle 1's stale, frozen session and "
        "never applied 'b' to the graph"
    )
    assert "a" in id_mapping_values

    assert "b" in _query_ids(store, "coll", 2)


# ---------------------------------------------------------------------------
# Defect 2: incremental publish trusts an unproven-complete session
# ---------------------------------------------------------------------------


def test_defect2_abort_resume_sequence_never_publishes_clean_over_untracked_mutations(
    tmp_path,
):
    """Abort/resume sequence (review brief Defect 2, scenario (a)).

    S1 begins, upserts several real points, then abort_indexing() discards
    it -- mutations are already durable on disk, only the in-memory
    session is discarded. A NEW session S2 begins (start_epoch = the
    dirty mutation_epoch left behind by the abort) and upserts ONE further
    point, complete_change_tracking=True.

    Pre-fix: end_indexing() trusts S2 as a complete record of every
    mutation since the last clean publish and applies ONLY S2's own
    tracked delta incrementally, publishing clean -- silently leaving S1's
    aborted points out of the HNSW graph forever, even though on disk.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("seed", "src/seed.py", 0)])
    _ = store.end_indexing("coll")

    store.begin_indexing("coll")
    aborted_points = [_point(f"abort{i}", f"src/abort{i}.py", 10 + i) for i in range(5)]
    _ = store.upsert_points("coll", aborted_points)
    assert _hnsw_sync(collection_path)["status"] == "dirty"
    store.abort_indexing("coll")
    assert _hnsw_sync(collection_path)["status"] == "dirty"

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("extra", "src/extra.py", 99)])
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    assert _hnsw_sync(collection_path)["status"] == "clean"
    id_mapping_values = _hnsw_id_mapping(collection_path)
    expected = {"extra", "seed"} | {f"abort{i}" for i in range(5)}
    assert expected <= id_mapping_values, (
        "an aborted session's already-durable mutations must still reach "
        "the HNSW graph -- pre-fix, the resuming session was wrongly "
        "trusted as a complete record of every mutation since the last "
        f"clean publish; missing: {expected - id_mapping_values}"
    )


def test_defect2_two_instances_interleaved_sequence_never_publishes_clean_over_missed_mutation(
    tmp_path,
):
    """Two-instance interleaved sequence (review brief Defect 2, scenario (b)).

    Two real FilesystemVectorStore instances against the SAME on-disk
    collection: instance A upserts 'a', instance B (a separate in-memory
    session) upserts 'b'. Instance A calls end_indexing() FIRST -- its own
    session only ever tracked 'a'.

    Pre-fix: A's end_indexing() trusts its own session and publishes the
    CURRENT on-disk mutation_epoch (already reflecting BOTH mutations) as
    clean, having applied only 'a' -- 'b' is silently, permanently missing
    even though the published state claims "clean".
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("seed", "src/seed.py", 0)])
    _ = store.end_indexing("coll")

    instance_a = store
    instance_b = FilesystemVectorStore(base_path=tmp_path)
    instance_a.begin_indexing("coll")
    instance_b.begin_indexing("coll")
    _ = instance_a.upsert_points("coll", [_point("a", "src/a.py", 1)])
    _ = instance_b.upsert_points("coll", [_point("b", "src/b.py", 2)])

    result_a = instance_a.end_indexing("coll")
    assert result_a["status"] == "ok"

    sync_after_a = _hnsw_sync(collection_path)
    id_mapping_after_a = _hnsw_id_mapping(collection_path)
    if sync_after_a["status"] == "clean":
        assert "b" in id_mapping_after_a, (
            "instance A published 'clean' without ever having tracked "
            "instance B's mutation of 'b' -- a session must never be "
            "trusted as complete when it did not observe every mutation "
            "since the last clean publish"
        )
    # A forced full rebuild (status left dirty, or a rebuild that captured
    # everything) is also an acceptable resolution -- the only invariant
    # under test is "never publish clean while missing a real mutation".


# ---------------------------------------------------------------------------
# Defect 3b: disabling the flag must also disable epoch bookkeeping writes
# ---------------------------------------------------------------------------


def test_defect3b_disabled_epoch_flag_never_writes_hnsw_sync_key(tmp_path):
    """AC46 cluster gate is not airtight: disabling
    hnsw_sync_epoch_enabled correctly forces a safe full rebuild (session
    is never created, mark_dirty/set_branch_context no-op), but pre-fix
    end_indexing() still unconditionally calls
    _resolve_and_publish_hnsw_sync() -> _finish_full_rebuild() ->
    _publish_clean_hnsw_sync(), which WRITES an hnsw_sync key into
    collection_meta.json regardless of the flag. A later, incorrectly
    postgres-unaware reader (e.g. an unfixed bypass) would then trust that
    epoch state as authoritative.
    """
    store = FilesystemVectorStore(base_path=tmp_path, hnsw_sync_epoch_enabled=False)
    collection_path = tmp_path / "coll"
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    assert "hnsw_sync" not in meta, (
        "hnsw_sync_epoch_enabled=False must skip the epoch-bookkeeping "
        "WRITE entirely, not just the filtering decision -- a leftover "
        "hnsw_sync key here would be wrongly trusted as authoritative by "
        "a later reader"
    )
    assert "a" in _query_ids(store, "coll", 1)


_FINDING2_UNBLOCKED_COMPLETION_WINDOW_SECONDS = 0.2
_FINDING2_THREAD_JOIN_TIMEOUT_SECONDS = 5


def test_finding2_write_hnsw_sync_state_must_respect_metadata_lock(tmp_path):
    """Finding 2 (opus, independently confirmed by the orchestrator via
    direct code read): ``write_hnsw_sync_state`` (called from
    ``_mark_hnsw_dirty_before_mutation``/``_publish_clean_hnsw_sync``) and
    ``HNSWIndexManager._update_metadata`` (called from
    ``save_incremental_update``/``rebuild_from_vectors``) both perform an
    independent read-merge-write of the SAME ``collection_meta.json`` file,
    but ``_update_metadata`` guards its critical section with
    ``.metadata.lock`` while ``write_hnsw_sync_state`` does not -- so they
    provide no mutual exclusion against each other, and a writer holding
    ``.metadata.lock`` (e.g. an in-progress ``save_incremental_update``)
    can be raced by a concurrent ``write_hnsw_sync_state`` call that
    silently interleaves its own read-merge-write in the middle.

    Proves this WITHOUT mocking any method of the class under test: a
    REAL ``.metadata.lock`` file is held externally via the SAME
    ``nfs_safe_flock`` utility ``HNSWIndexManager._update_metadata`` uses
    (real OS-level flock, real file, no monkeypatching), then a REAL
    ``write_hnsw_sync_state`` call is made on a separate thread. Pre-fix,
    that call is NOT blocked by the held lock and completes within a short
    window. Post-fix, it must block until the lock is released.

    Must fail on current code (the write completes while the lock is
    still held) and pass once ``write_hnsw_sync_state`` also acquires
    ``.metadata.lock``.
    """
    import fcntl
    import threading

    from code_indexer.storage.shared.hnsw_sync_state import (
        HNSW_SYNC_SCHEMA_VERSION,
        HNSWSyncState,
        read_hnsw_sync_state,
        write_hnsw_sync_state,
    )
    from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

    store = FilesystemVectorStore(base_path=tmp_path)
    collection_name = "coll"
    collection_path = tmp_path / collection_name
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, [_point("a", "src/a.py", 1)])
    store.end_indexing(collection_name)

    baseline_sync = read_hnsw_sync_state(collection_path)
    assert baseline_sync is not None, "test setup invalid: no baseline hnsw_sync state"

    lock_file = collection_path / ".metadata.lock"
    lock_file.touch(exist_ok=True)

    completed_event = threading.Event()
    write_errors: list = []

    def _run_dirty_write():
        try:
            new_state = HNSWSyncState(
                schema_version=HNSW_SYNC_SCHEMA_VERSION,
                mutation_epoch=baseline_sync.mutation_epoch + 1,
                published_epoch=baseline_sync.published_epoch,
                status="dirty",
                current_branch=baseline_sync.current_branch,
                layout=baseline_sync.layout,
            )
            write_hnsw_sync_state(collection_path, new_state)
        except Exception as exc:  # noqa: BLE001
            write_errors.append(exc)
        finally:
            completed_event.set()

    with open(lock_file, "r+") as lock_f:
        used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            writer_thread = threading.Thread(target=_run_dirty_write)
            writer_thread.start()

            unblocked_before_release = completed_event.wait(
                timeout=_FINDING2_UNBLOCKED_COMPLETION_WINDOW_SECONDS
            )
        finally:
            nfs_safe_funlock(lock_f.fileno(), used_lockf)

    writer_thread.join(timeout=_FINDING2_THREAD_JOIN_TIMEOUT_SECONDS)
    assert not writer_thread.is_alive(), "writer thread failed to terminate in time"
    assert not write_errors, f"write_hnsw_sync_state raised: {write_errors}"

    assert not unblocked_before_release, (
        "write_hnsw_sync_state completed WHILE .metadata.lock was still "
        "held externally -- it provides no mutual exclusion against "
        "HNSWIndexManager._update_metadata's real lock, so the two "
        "writers can interleave and lose an update to the same "
        "collection_meta.json file"
    )

    final_sync = read_hnsw_sync_state(collection_path)
    assert final_sync is not None
    assert final_sync.mutation_epoch == baseline_sync.mutation_epoch + 1
