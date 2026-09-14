"""Concurrency regression test for ABBA deadlock between _path_index_lock and _id_index_lock.

BLOCKER B1: upsert_points acquires _path_index_lock (outer) then _id_index_lock (inner).
            delete_points acquires _id_index_lock (outer) then _path_index_lock (inner).
            Running both simultaneously can cause deadlock.

This test spawns two threads — one upserts, one deletes — and asserts both
complete within a generous wall-clock budget.

Bug #1575 round 6/7 (Gap D/B out-of-session PathIndex persistence,
``_persist_out_of_session_path_index`` + ``_mark_hnsw_dirty_before_mutation``)
made every out-of-session ``upsert_points``/``delete_points`` call (this
test never opens a ``begin_indexing`` session, deliberately exercising the
out-of-session path the Gap D/B fix targets) perform several synchronous,
fsync'd durable writes -- path_index.bin, its co-persisted id_index.bin,
and hnsw_sync_state.json -- instead of the near-zero-cost in-memory
mutation this test's original "single-digit seconds" docstring line was
measured against. This is an intentional, dual-reviewed correctness
tradeoff (crash-durability for out-of-session mutations), NOT a
performance regression to fix here. Measured real wall-clock time for
this test's actual call volume is now ~1.5s (see the ITERATIONS/
INITIAL_FILES constants below for the Bug #1823 sizing rationale).

Following the SAME pattern already established by sibling concurrency
tests from this exact fix set
(``test_filesystem_vector_store_1575_round3_gap_c_concurrency.py``):
``WORKER_TIMEOUT_SECONDS`` bounds each individual ``Future.result()`` call
(a plain Python `ThreadPoolExecutor` cannot forcibly kill a hung thread,
so this is a courtesy value, not itself a hard guarantee), while the outer
``pytest.mark.timeout(TEST_TIMEOUT_SECONDS)`` is the actual hard wall-clock
ceiling -- pytest-timeout terminates the test PROCESS outright on a
genuine deadlock, instead of hanging the suite. Both are sized with
generous headroom above the ~1.5s observed range so that a real,
reintroduced ABBA deadlock (which hangs forever, independent of any
timeout value chosen here) is still caught reliably, just as before this
change. Bug #1823 also added a SEPARATE, deterministic ABBA reproduction
below (`TestDeterministicAbbaLockOrder`) that forces the exact overlap on
every single run instead of relying on this probabilistic test's real
GIL scheduling -- see that class's own docstring, and this file's
per-test docstring below, for the full discrimination story.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 32
COLLECTION = "lock_order_test"
# Bug #1823: originally lowered from 100 to 15 (see git history for that
# investigation), now lowered further to 5. An ABBA deadlock is a
# STRUCTURAL bug (either the code can deadlock or it structurally
# cannot); the investigation (25 consecutive real pytest runs of this
# exact test against genuinely reverted B1-shape code, ALL staying green
# -- see `TestDeterministicAbbaLockOrder`'s own docstring for the full
# story) proved iteration count does not materially change this test's
# already-low natural reproduction odds for its specific in-memory,
# brand-new-file call pattern. Since `TestDeterministicAbbaLockOrder`
# (landed below) now carries FULL, deterministic detection responsibility
# for the ABBA class (5/5 RED against reverted code, 25/25 GREEN against
# the fix -- see that class's docstring), this test's remaining job is
# only a cheap smoke check: real call paths under real concurrent load,
# asserting no deadlock/exception within a generous timeout. That role
# does not need 15 iterations either -- measured: dropping to 5 brings
# this test's isolated wall time from ~5-6.4s down to ~1.5s, which is
# what actually keeps this file under the file-level 5s budget once
# TestDeterministicAbbaLockOrder's own ~1.3s is added on top.
# INITIAL_FILES is kept equal to ITERATIONS so the delete loop still
# consumes one batch of CHUNKS_PER_FILE ids per iteration without
# early-breaking.
ITERATIONS = 5
INITIAL_FILES = 5
CHUNKS_PER_FILE = 4
NEW_FILE_CHUNK_COUNT = 2
BARRIER_TIMEOUT_SECONDS = 10

# See module docstring: out-of-session upsert/delete calls now legitimately
# cost several synchronous fsync'd writes each (Bug #1575 Gap D/B).
# WORKER_TIMEOUT_SECONDS/TEST_TIMEOUT_SECONDS are kept at 30/45 (unchanged
# by the ITERATIONS 15->5 reduction above) -- they are hard backstops that
# fire ONLY on a genuine hang, never on a normal passing run, so they do
# not need to track this test's actual observed wall-clock time (now
# ~1.5s isolated) as closely; the generous headroom is what guarantees a
# genuinely reintroduced deadlock is still caught reliably.
WORKER_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 45


def _make_vector() -> np.ndarray:
    return np.random.rand(VECTOR_SIZE).astype(np.float32)


def _make_point(file_path: str, chunk_idx: int, point_id: str) -> Dict:
    return {
        "id": point_id,
        "vector": _make_vector(),
        "payload": {
            "path": file_path,
            "type": "content",
            "chunk_index": chunk_idx,
        },
    }


def _populate_initial(
    store: FilesystemVectorStore,
) -> Dict[str, List[str]]:
    """Populate store with INITIAL_FILES * CHUNKS_PER_FILE points.

    Returns mapping of file_path -> [point_ids].
    """
    file_to_ids: Dict[str, List[str]] = {}
    for i in range(INITIAL_FILES):
        fp = f"src/init_file_{i:04d}.py"
        ids = []
        points = []
        for j in range(CHUNKS_PER_FILE):
            pid = f"init_{i:04d}_chunk{j}"
            ids.append(pid)
            points.append(_make_point(fp, j, pid))
        store.upsert_points(COLLECTION, points)
        file_to_ids[fp] = ids
    return file_to_ids


def _record_error(
    errors: List[str], errors_lock: threading.Lock, label: str, exc: Exception
) -> None:
    with errors_lock:
        errors.append(f"{label}: {exc}")


def _run_upsert_loop(
    store: FilesystemVectorStore,
    barrier: threading.Barrier,
    errors: List[str],
    errors_lock: threading.Lock,
) -> None:
    """Loop upsert_points ITERATIONS times with fresh files."""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        for k in range(ITERATIONS):
            fp = f"src/new_file_{uuid.uuid4().hex[:8]}.py"
            pts = [
                _make_point(fp, j, f"new_{k}_{j}") for j in range(NEW_FILE_CHUNK_COUNT)
            ]
            store.upsert_points(COLLECTION, pts)
    except Exception as exc:
        _record_error(errors, errors_lock, "upsert thread", exc)


def _run_delete_loop(
    store: FilesystemVectorStore,
    initial_ids: List[str],
    barrier: threading.Barrier,
    errors: List[str],
    errors_lock: threading.Lock,
) -> None:
    """Loop delete_points ITERATIONS times on the initial population."""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        # Work through the initial ids in batches of CHUNKS_PER_FILE
        idx = 0
        for _ in range(ITERATIONS):
            batch = initial_ids[idx : idx + CHUNKS_PER_FILE]
            if not batch:
                break
            store.delete_points(COLLECTION, batch)
            idx += CHUNKS_PER_FILE
    except Exception as exc:
        _record_error(errors, errors_lock, "delete thread", exc)


class TestConcurrentUpsertAndDeleteNoDeadlock:
    """Deadlock regression: upsert_points and delete_points must not ABBA deadlock."""

    @pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
    def test_concurrent_upsert_and_delete_no_deadlock(self, tmp_path: Path) -> None:
        """Two threads running upsert_points and delete_points concurrently
        must both complete within a generous wall-clock budget.

        Bug #1823 (truthfulness correction): this test exercises the REAL
        `upsert_points`/`delete_points` call paths under real concurrent
        load and asserts no deadlock/exception within a generous timeout
        -- it is NOT a reliable ABBA detector. The module docstring's own
        investigation confirmed 25 consecutive runs against genuinely
        reverted (ABBA-buggy) lock-nesting code ALL stayed green for this
        call pattern; CPython's GIL essentially never switches threads
        inside the tiny nested-lock window this test's brand-new-file
        upsert/delete pattern reaches. If the buggy code were ever
        reintroduced, this test would most likely stay green rather than
        hang. `TestDeterministicAbbaLockOrder` below is the test that
        reliably catches a reintroduced ABBA violation, on every single
        run, deterministically.
        """
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)

        file_to_ids = _populate_initial(store)
        initial_ids: List[str] = [pid for ids in file_to_ids.values() for pid in ids]

        # Barrier ensures both threads enter their hot loop at the same time,
        # maximising the chance of interleaving that triggers the deadlock.
        barrier = threading.Barrier(2)
        errors: List[str] = []
        errors_lock = threading.Lock()

        # ThreadPoolExecutor + Future.result() propagates a worker exception
        # to the calling (test) thread. WORKER_TIMEOUT_SECONDS bounds each
        # result() call as a courtesy; the real hard backstop against a
        # genuine deadlock is the outer @pytest.mark.timeout above (see
        # module docstring -- matches the established sibling-test
        # convention in this exact fix set).
        with ThreadPoolExecutor(max_workers=2) as executor:
            upsert_future = executor.submit(
                _run_upsert_loop, store, barrier, errors, errors_lock
            )
            delete_future = executor.submit(
                _run_delete_loop, store, initial_ids, barrier, errors, errors_lock
            )
            upsert_future.result(timeout=WORKER_TIMEOUT_SECONDS)
            delete_future.result(timeout=WORKER_TIMEOUT_SECONDS)

        assert not errors, f"Thread errors detected: {errors}"


# Bug #1823 Finding H1: deterministic ABBA reproduction, landing the
# investigation the module docstring describes (see its "SEPARATE
# deterministic check" paragraph) instead of leaving it a discarded
# one-off experiment.
_DETERMINISTIC_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_DETERMINISTIC_JOIN_TIMEOUT_SECONDS = 5.0


class _RendezvousLock:
    """Wraps a REAL `threading.Lock` -- specifically, the store's actual
    `_path_index_lock` or `_id_index_lock` object, assigned onto a live
    `FilesystemVectorStore` instance in place of the plain lock it
    constructed itself. Forwards `acquire`/`release` unchanged, EXCEPT:
    the first time the DESIGNATED thread (identified by
    `threading.get_ident()`, captured once that thread has actually
    started -- see `designated_ident_holder`) acquires THIS specific
    wrapped lock, it runs a two-phase handshake (`_rendezvous`) before
    returning control to the caller: (1) release the real lock and wait
    (bounded) for `sibling_ready_event`, so an unrelated non-designated
    touch of this SAME lock by the sibling thread (e.g. upsert_points'
    early "ensure ID index exists" acquisition, which precedes its own
    designated outer-lock acquisition) is never artificially blocked for
    the whole handshake window; (2) re-acquire the real lock, then
    rendezvous on a SHARED `post_reacquire_barrier` so BOTH threads are
    guaranteed to hold their real outer lock simultaneously the instant
    either resumes.

    Two such wrappers, one per lock, cross-wired (each one's
    `ready_event` is the other's `sibling_ready_event`, and both share
    the SAME `post_reacquire_barrier`) deterministically force both
    threads to be simultaneously holding their real OUTER lock before
    either attempts its nested lock -- constructing the exact structural
    precondition for an ABBA deadlock on every single run, instead of
    hoping CPython's GIL happens to interleave inside the narrow natural
    window (confirmed NOT to happen across 25 real runs of the
    probabilistic call-pattern test above -- see the module docstring).
    Both phases were empirically required (Bug #1823): a single-phase
    hold-during-wait design deadlocked the FIXED, correct production code
    ~50-70% of the time (an unrelated non-designated lock touch); a
    single re-acquire with no second barrier only detected the real ABBA
    deadlock 3/5 times (one thread could race ahead and finish before the
    other even attempted its nested acquisition).

    Every OTHER acquisition -- a different thread, or a repeat
    acquisition by the SAME designated thread (e.g. a later, unrelated
    lock use later in the same call) -- forwards straight to the real
    lock with no handshake, via the one-shot `_handshake_done` guard.
    Neither `FilesystemVectorStore`'s methods, `PathIndex`, nor the lock
    TYPE are mocked; this is a thin forwarding seam around the real
    `threading.Lock` instance the store already owns, mirroring the
    `_RaceInjectingConnection` timing seam used for the sqlite TOCTOU
    race elsewhere in this test suite (Bug #1823 Finding B1/B2).
    """

    def __init__(
        self,
        real_lock: threading.Lock,
        designated_ident_holder: List[Optional[int]],
        ready_event: threading.Event,
        sibling_ready_event: threading.Event,
        post_reacquire_barrier: threading.Barrier,
        handshake_timeout: float,
    ) -> None:
        self._real_lock = real_lock
        self._designated_ident_holder = designated_ident_holder
        self._ready_event = ready_event
        self._sibling_ready_event = sibling_ready_event
        self._post_reacquire_barrier = post_reacquire_barrier
        self._handshake_timeout = handshake_timeout
        self._handshake_done = False

    def acquire(self, *args, **kwargs) -> bool:
        acquired = self._real_lock.acquire(*args, **kwargs)
        is_designated = threading.get_ident() == self._designated_ident_holder[0]
        if acquired and not self._handshake_done and is_designated:
            self._handshake_done = True
            acquired = self._rendezvous(*args, **kwargs)
        return acquired

    def _rendezvous(self, *args, **kwargs) -> bool:
        """Two-phase handshake -- see the class docstring for the full
        empirically-derived rationale (Bug #1823): (1) release the real
        lock while waiting for the sibling's own designated acquisition,
        so an unrelated non-designated touch of this SAME lock by the
        sibling thread is never artificially blocked; (2) after
        re-acquiring, rendezvous on a SHARED barrier so both threads are
        guaranteed to hold their real outer lock simultaneously the
        instant either resumes -- without this second point, independent
        re-acquisition let one thread finish its whole nested critical
        section before the other even attempted its own.
        """
        self._ready_event.set()
        self._real_lock.release()
        if not self._sibling_ready_event.wait(timeout=self._handshake_timeout):
            raise TimeoutError(
                "_RendezvousLock: sibling never reached its own designated "
                "acquisition -- test-infrastructure failure, not evidence "
                "of a production deadlock"
            )
        acquired = self._real_lock.acquire(*args, **kwargs)
        if acquired:
            try:
                self._post_reacquire_barrier.wait(timeout=self._handshake_timeout)
            except threading.BrokenBarrierError:
                # Sibling never reached the barrier -- release the
                # just-reacquired lock rather than exit still holding it.
                self._real_lock.release()
                raise
        return acquired

    def release(self) -> None:
        self._real_lock.release()

    def __enter__(self) -> "_RendezvousLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class TestDeterministicAbbaLockOrder:
    """Bug #1823 Finding H1: lands the deterministic ABBA reproduction the
    investigation built and proved (see the module docstring's "SEPARATE
    deterministic check" paragraph) instead of leaving it a discarded
    one-off script.

    Unlike `test_concurrent_upsert_and_delete_no_deadlock` above (which
    relies on real GIL scheduling to interleave two real
    `upsert_points()`/`delete_points()` calls and, per the module
    docstring's own investigation, essentially never lands inside the
    narrow ABBA window for its call pattern), this test forces the exact
    overlap deterministically: `_RendezvousLock` (a thin forwarding seam
    around the store's REAL `_path_index_lock`/`_id_index_lock` objects,
    never a mock of either lock or of `FilesystemVectorStore`) makes the
    upsert thread rendezvous at its real outer `_path_index_lock`
    acquisition (`upsert_points`'s STEP1 orphan-handling block) and the
    delete thread rendezvous at its real outer `_id_index_lock`
    acquisition (`delete_points`'s SHARDED_JSON branch), guaranteeing
    both threads hold their respective outer lock simultaneously before
    either attempts its nested lock -- on every single run, not
    probabilistically.
    """

    def test_upsert_and_delete_rendezvous_at_outer_locks_completes_without_deadlock(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
        # Bug #1823: an IN-SESSION collection (collection_name present in
        # self._indexing_session_changes) makes both upsert_points() and
        # delete_points() skip their end-of-call
        # _persist_out_of_session_path_index() (Bug #1575 round 6 Gap
        # D/B) -- a SEPARATE contention point on these SAME two locks
        # (via _rebuild_and_repair_path_index()'s authoritative rescan on
        # a collection with no prior path_index.bin) that is unrelated to
        # the STEP1/delete-branch ABBA shape this test targets. Without
        # this, the test measurably flaked on the FIXED, correct
        # production code (observed 2/5 runs hanging) due to that
        # unrelated contention, not a real ABBA deadlock.
        store.begin_indexing(COLLECTION)

        target_file = "src/deterministic_target.py"
        other_file = "src/deterministic_other.py"
        # Two pre-existing points on target_file: the race upsert below
        # replaces them with a DIFFERENT single point id, guaranteeing
        # non-empty orphan_point_ids -- the only condition under which
        # upsert_points' SHARDED_JSON STEP1 block nests _id_index_lock
        # inside _path_index_lock at all.
        store.upsert_points(
            COLLECTION,
            [
                _make_point(target_file, 0, "orig_a"),
                _make_point(target_file, 1, "orig_b"),
            ],
        )
        # A real, independently-deletable point whose file_path is
        # resolvable from its own vector JSON, for the delete thread.
        store.upsert_points(COLLECTION, [_make_point(other_file, 0, "other_x")])

        upsert_ident: List[Optional[int]] = [None]
        delete_ident: List[Optional[int]] = [None]
        path_ready = threading.Event()
        id_ready = threading.Event()
        # Shared by BOTH wrappers -- see _RendezvousLock's docstring for
        # why a second synchronization point is required.
        post_reacquire_barrier = threading.Barrier(2)

        # path_lock is upsert_points' real outer lock; id_lock is
        # delete_points' real outer lock (see both methods' SHARDED_JSON
        # branches). Cross-wired: each wrapper's own ready_event is the
        # other's sibling_ready_event.
        #
        # mypy flags these as incompatible with the declared
        # `threading.Lock` attribute type -- that IS the point:
        # `_RendezvousLock` is a deliberate, test-only duck-typed proxy
        # (acquire/release/__enter__/__exit__) substituted in place of
        # the store's real lock object, exactly mirroring the
        # established `_RaceInjectingConnection` seam pattern elsewhere
        # in this test suite.
        store._path_index_lock = _RendezvousLock(  # type: ignore[assignment]
            store._path_index_lock,
            upsert_ident,
            path_ready,
            id_ready,
            post_reacquire_barrier,
            _DETERMINISTIC_HANDSHAKE_TIMEOUT_SECONDS,
        )
        store._id_index_lock = _RendezvousLock(  # type: ignore[assignment]
            store._id_index_lock,
            delete_ident,
            id_ready,
            path_ready,
            post_reacquire_barrier,
            _DETERMINISTIC_HANDSHAKE_TIMEOUT_SECONDS,
        )

        errors: List[str] = []
        errors_lock = threading.Lock()

        def run_upsert() -> None:
            upsert_ident[0] = threading.get_ident()
            try:
                store.upsert_points(COLLECTION, [_make_point(target_file, 0, "new_a")])
            except Exception as exc:
                _record_error(errors, errors_lock, "upsert thread", exc)

        def run_delete() -> None:
            delete_ident[0] = threading.get_ident()
            try:
                store.delete_points(COLLECTION, ["other_x"])
            except Exception as exc:
                _record_error(errors, errors_lock, "delete thread", exc)

        upsert_thread = threading.Thread(target=run_upsert, daemon=True)
        delete_thread = threading.Thread(target=run_delete, daemon=True)
        upsert_thread.start()
        delete_thread.start()
        upsert_thread.join(timeout=_DETERMINISTIC_JOIN_TIMEOUT_SECONDS)
        delete_thread.join(timeout=_DETERMINISTIC_JOIN_TIMEOUT_SECONDS)

        still_alive = [
            name
            for name, t in (("upsert", upsert_thread), ("delete", delete_thread))
            if t.is_alive()
        ]
        assert not still_alive, (
            f"{still_alive} thread(s) still alive after "
            f"{_DETERMINISTIC_JOIN_TIMEOUT_SECONDS}s -- ABBA deadlock between "
            f"_path_index_lock and _id_index_lock, deterministically "
            f"reproduced via forced rendezvous at both real outer lock "
            f"acquisitions"
        )
        assert not errors, f"Thread errors detected: {errors}"
