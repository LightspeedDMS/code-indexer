"""TDD tests for Bug #1575 Part C -- locking scope (AC24/AC45) and crash
recovery (AC26/AC27/AC27b).

Real FilesystemVectorStore + real HNSW + real filesystem, real threads for
the concurrency proof -- no mocking of the code under test. Crash windows
that cannot be induced by literally killing a process are simulated by
directly manipulating on-disk state to the EXACT shape a crash at that
point would leave behind (documented at each test).
"""

import fcntl
import json
import threading
import time

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

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


def _hnsw_index_identity(collection_path):
    st = (collection_path / "hnsw_index.bin").stat()
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _query_ids(store, collection_name, seed, limit=50):
    results = store.search(
        query="unused",
        embedding_provider=_UnusedEmbeddingProvider(),
        collection_name=collection_name,
        limit=limit,
        precomputed_query_vector=_vector(seed),
    )
    return {r["id"] for r in results}


# --- AC45: lock released between dirty-write/mutation and returning -------


@pytest.mark.timeout(10)
def test_ac45_lock_released_after_upsert_points_returns(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])

    # The lock must be fully released by the time upsert_points() returns --
    # a fresh, independent, NON-BLOCKING acquisition attempt must succeed
    # immediately.
    lock_file = tmp_path / "coll" / ".index_rebuild.lock"
    lock_file.touch(exist_ok=True)
    with open(lock_file, "r+") as lock_f:
        used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            pass  # Acquisition succeeding (no exception) is the proof.
        finally:
            nfs_safe_funlock(lock_f.fileno(), used_lockf)


# --- AC24: concurrent mutations correctly serialize, no lost updates ------


@pytest.mark.timeout(30)
def test_ac24_concurrent_upserts_serialize_epoch_with_no_lost_updates(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    errors: list = []
    errors_lock = threading.Lock()

    def _worker(i):
        try:
            barrier.wait(timeout=5)
            store.upsert_points("coll", [_point(f"p{i}", f"src/f{i}.py", i)])
        except Exception as exc:  # pragma: no cover - failure path
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    for t in threads:
        assert not t.is_alive(), "a worker thread failed to terminate in time"

    assert not errors, f"Concurrent upserts raised: {errors}"
    sync_state = _hnsw_sync(tmp_path / "coll")
    assert sync_state["mutation_epoch"] == thread_count, (
        "every concurrent mutation must bump the epoch exactly once -- a "
        "lower count would mean a lost update (race), a higher count "
        "would mean double-counting"
    )


# --- AC50: a racing mutation can never be absorbed into a stale "clean" ----

_AC50_CONTENTION_POLL_MAX_ATTEMPTS = 2000  # bounded (Messi #14): ~2s @ 1ms/poll
_AC50_CONTENTION_POLL_INTERVAL_SECONDS = 0.001
_AC50_JOIN_TIMEOUT_SECONDS = 15
_AC50_SEED_POINT_COUNT = 60
_AC50_TEST_TIMEOUT_SECONDS = 20
_AC50_MUTATION_POINT_ID = "b"
_AC50_MUTATION_POINT_PATH = "src/b.py"
_AC50_MUTATION_POINT_SEED = 2


def _wait_for_lock_contention(lock_file_path) -> bool:
    """Bounded poll (Messi #14): trylocks the given lock file until
    acquisition fails with OSError (contention proven) or the bound is
    exhausted. Returns True iff contention was observed."""
    lock_file_path.touch(exist_ok=True)
    for _ in range(_AC50_CONTENTION_POLL_MAX_ATTEMPTS):
        with open(lock_file_path, "r+") as probe_fd:
            try:
                used_lockf = nfs_safe_flock(
                    probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except OSError:
                return True
            # nfs_safe_funlock's signature returns None -- nothing to check.
            nfs_safe_funlock(probe_fd.fileno(), used_lockf)
        time.sleep(_AC50_CONTENTION_POLL_INTERVAL_SECONDS)
    return False


def _ac50_bootstrap_collection(tmp_path):
    """Seeds 'coll' so the bootstrap full rebuild takes measurably longer
    than an instant, widening the window for the contention-poll."""
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll",
        [
            _point(f"seed{i}", f"src/seed{i}.py", i)
            for i in range(_AC50_SEED_POINT_COUNT)
        ],
    )
    _ = store.end_indexing("coll")  # bootstrap: clean, published_epoch=1
    return store


def _safe_run(fn, errors, errors_lock):
    try:
        fn()
    except Exception as exc:  # pragma: no cover - failure path
        with errors_lock:
            errors.append(exc)


def _mutation_after_contention(
    other_store, lock_file_path, contention_observed, errors, errors_lock
):
    def _inner():
        if not _wait_for_lock_contention(lock_file_path):
            raise RuntimeError(
                "never observed end_indexing() holding the lock within "
                "the bounded poll window"
            )
        contention_observed.set()
        other_store.upsert_points(
            "coll",
            [
                _point(
                    _AC50_MUTATION_POINT_ID,
                    _AC50_MUTATION_POINT_PATH,
                    _AC50_MUTATION_POINT_SEED,
                )
            ],
        )

    _safe_run(_inner, errors, errors_lock)


def _run_ac50_race(store, other_store, lock_file_path):
    """Runs end_indexing() and a contention-gated concurrent mutation on
    separate real threads -- the mutation only acts once it has directly
    observed the lock already held, making the ordering deterministic
    rather than a bare thread-start race. Returns (errors,
    contention_observed)."""
    contention_observed = threading.Event()
    errors: list = []
    errors_lock = threading.Lock()

    t1 = threading.Thread(
        target=_safe_run,
        args=(lambda: store.end_indexing("coll"), errors, errors_lock),
    )
    t2 = threading.Thread(
        target=_mutation_after_contention,
        args=(other_store, lock_file_path, contention_observed, errors, errors_lock),
    )
    t1.start()
    t2.start()
    t1.join(timeout=_AC50_JOIN_TIMEOUT_SECONDS)
    t2.join(timeout=_AC50_JOIN_TIMEOUT_SECONDS)
    assert not t1.is_alive() and not t2.is_alive(), (
        "a worker thread failed to terminate in time"
    )
    return errors, contention_observed


def _assert_ac50_race_result(errors, contention_observed, collection_path):
    assert not errors, f"Concurrent operations raised: {errors}"
    assert contention_observed.is_set(), (
        "the mutation thread must have directly observed lock contention "
        "before proceeding -- otherwise this test proves nothing"
    )
    sync_after_race = _hnsw_sync(collection_path)
    assert sync_after_race["status"] == "dirty", (
        "a racing mutation must never be silently absorbed into a stale "
        "'clean' publication"
    )
    assert sync_after_race["mutation_epoch"] > sync_after_race["published_epoch"]


@pytest.mark.timeout(_AC50_TEST_TIMEOUT_SECONDS)
def test_ac50_concurrent_mutation_during_end_indexing_cannot_be_published_as_stale_clean(
    tmp_path,
):
    """A mutation landing after end_indexing()'s decision snapshot but
    before its publication must never be silently dropped -- the design
    holds .index_rebuild.lock continuously across read-decide-build-
    publish (AC24/AC45), so a racing dirty-before-write can only land
    strictly before or strictly after that whole sequence, never inside.
    """
    store = _ac50_bootstrap_collection(tmp_path)
    collection_path = tmp_path / "coll"
    lock_file_path = collection_path / ".index_rebuild.lock"

    store.begin_indexing("coll")  # zero mutations this session so far
    other_store = FilesystemVectorStore(base_path=tmp_path)
    other_store.begin_indexing("coll")

    errors, contention_observed = _run_ac50_race(store, other_store, lock_file_path)
    _assert_ac50_race_result(errors, contention_observed, collection_path)

    # A fresh refresh recovers correctly -- "b" is not lost.
    other_store.end_indexing("coll")
    assert _AC50_MUTATION_POINT_ID in _query_ids(
        other_store, "coll", _AC50_MUTATION_POINT_SEED
    )


# --- AC26: crash after mutation but before HNSW publication self-heals ----


def test_ac26_dirty_state_with_completed_mutation_self_heals_via_full_rebuild(
    tmp_path,
):
    """Simulates: the dirty-before-write completed, the storage mutation
    (vector file write) completed, but the process died before
    end_indexing()'s HNSW build/publish phase ever ran. The NEXT
    end_indexing() call (fresh process/store) must observe status=dirty and
    perform a full rebuild rather than concluding "nothing changed".

    Asserts the HNSW ARTIFACT itself (vector_count/id_mapping in
    collection_meta.json) correctly includes every vector present on disk
    after recovery. This deliberately does NOT assert via search() that
    both points are end-to-end queryable: doing so exposed a SEPARATE,
    PRE-EXISTING bug (verified byte-identical against commit fa1e104a,
    predating Bug #1575 Part C entirely) in SHARDED_JSON's
    ``_load_id_index()`` -- ``id_index.bin`` only refreshes from its
    persisted binary file (never a fresh disk rescan once that file
    already exists), so a crash between an upsert and ITS OWN
    end_indexing() call can leave id_index.bin stale relative to a
    subsequent full HNSW rebuild. That gap is out of scope for this fix
    and is disclosed separately, not silently worked around here.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    _ = store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    identity_before_second_mutation = _hnsw_index_identity(collection_path)

    # Simulate the crash: perform the storage mutation for a SECOND point
    # via a fresh store instance (its OWN dirty-before-write correctly
    # fires), but never call end_indexing() for it -- the process "dies"
    # here, leaving hnsw_sync durably dirty with the mutation on disk.
    crashed_store = FilesystemVectorStore(base_path=tmp_path)
    crashed_store.begin_indexing("coll")
    _ = crashed_store.upsert_points("coll", [_point("b", "src/b.py", 2)])
    # NO end_indexing() call -- simulating the crash.

    sync_state = _hnsw_sync(collection_path)
    assert sync_state["status"] == "dirty"

    # A fresh recovery process observes dirty state and must self-heal.
    recovery_store = FilesystemVectorStore(base_path=tmp_path)
    recovery_store.begin_indexing("coll")
    _ = recovery_store.end_indexing("coll")

    identity_after_recovery = _hnsw_index_identity(collection_path)
    assert identity_after_recovery != identity_before_second_mutation, (
        "recovery must perform a real rebuild, not silently skip"
    )
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["status"] == "clean"

    # The HNSW ARTIFACT correctly includes BOTH vectors present on disk --
    # proving the full rebuild scanned the real filesystem state, not a
    # stale in-memory/cached view.
    hnsw_info = json.loads((collection_path / "collection_meta.json").read_text())[
        "hnsw_index"
    ]
    assert hnsw_info["vector_count"] == 2
    assert set(hnsw_info["id_mapping"].values()) == {"a", "b"}


# --- AC27: crash after HNSW publish but before clean hnsw_sync write -----


def test_ac27_hnsw_published_but_sync_still_dirty_self_heals(tmp_path):
    """Simulates: the HNSW file was fully written and durable, but the
    process died before the final clean hnsw_sync write landed -- the
    on-disk state remains dirty even though the artifact is (coincidentally)
    already correct. The next refresh must NOT trust that coincidence; it
    must still perform a fresh, provably-correct rebuild.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    _ = store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    sync_file = collection_path / "hnsw_sync_state.json"
    sync_state = json.loads(sync_file.read_text())
    # Force the durable state back to "dirty" without touching the (already
    # valid) HNSW artifact -- simulating a crash between HNSW publication
    # and the clean hnsw_sync write. Bug #1619: hnsw_sync now lives in its
    # own dedicated file, not embedded in collection_meta.json.
    sync_state["status"] = "dirty"
    sync_state["mutation_epoch"] = sync_state["published_epoch"] + 1
    sync_file.write_text(json.dumps(sync_state))

    identity_before = _hnsw_index_identity(collection_path)

    recovery_store = FilesystemVectorStore(base_path=tmp_path)
    recovery_store.begin_indexing("coll")
    _ = recovery_store.end_indexing("coll")

    sync_after = _hnsw_sync(collection_path)
    assert sync_after["status"] == "clean"
    # A full rebuild ran (never blindly trusted the dirty-but-actually-fine
    # state) -- proven by a fresh artifact identity.
    assert _hnsw_index_identity(collection_path) != identity_before


# --- Defensive fix: decision-engine failure discards the in-memory session -


def test_decision_engine_failure_discards_session_not_leave_it_stale(tmp_path):
    """When end_indexing()'s decision engine itself raises (simulated here
    by deleting collection_meta.json after a mutation already created a
    session but before end_indexing() runs, so the internal HNSW rebuild's
    own fresh metadata read fails), the in-memory HNSWSyncSession for that
    collection must be discarded -- never left lingering with stale
    added/updated tracking sets that a SUBSEQUENT begin_indexing()/
    upsert_points() call in the SAME process could incorrectly reuse
    against a freshly-reset _indexing_session_changes generation.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    _ = store.end_indexing("coll")  # vector_size gets cached for "coll"

    collection_path = tmp_path / "coll"

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("b", "src/b.py", 2)])

    session_key = store._hnsw_sync_session_key(collection_path)
    assert session_key in store._hnsw_sync_sessions

    # Simulate corruption: collection_meta.json vanishes after the mutation
    # (and its dirty-write) already succeeded, but before end_indexing()'s
    # own internal rebuild tries to re-read it.
    (collection_path / "collection_meta.json").unlink()

    with pytest.raises(Exception):
        store.end_indexing("coll")

    assert session_key not in store._hnsw_sync_sessions, (
        "a failed decision-engine attempt must discard the in-memory "
        "session rather than leave it stale for the next attempt"
    )


# --- AC27b: partial incremental failure discards the attempt --------------


def test_ac27b_partial_incremental_failure_falls_back_to_full_rebuild(tmp_path):
    """A point tracked as 'added' this session whose underlying record
    vanishes before the incremental apply reads it (simulating a failure
    mid-application) must abort the incremental attempt entirely and fall
    back to a fresh full rebuild -- never publish a partial index.

    Proven two ways: (1) result["hnsw_update"] is ABSENT -- this key is
    ONLY ever set when the decision engine's action is the incremental one
    (see end_indexing()'s exact contract), so its absence here is direct
    proof the incremental attempt did not successfully complete and the
    engine fell back to a full rebuild instead of silently publishing a
    partial index; (2) the survivor point remains correctly queryable.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    _ = store.end_indexing("coll")

    collection_path = tmp_path / "coll"

    # Second session: add a real point "b", but then delete its underlying
    # vector file directly (simulating "the record vanished mid-apply")
    # BEFORE end_indexing()'s incremental path tries to read it.
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("b", "src/b.py", 2)])

    vector_files = list(collection_path.rglob("vector_b*.json"))
    assert vector_files, "expected to find b's vector file on disk"
    for f in vector_files:
        f.unlink()

    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    assert "hnsw_update" not in result, (
        "absence proves the incremental path did NOT complete successfully "
        "-- the decision engine fell back to a full rebuild instead"
    )
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["status"] == "clean"

    # "a" must still be queryable -- a fresh full rebuild recovered
    # correctly (never a published partial/broken index).
    assert _query_ids(store, "coll", 1) == {"a"}
