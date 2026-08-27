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
performance regression to fix here. Measured real wall-clock time for this
test's actual call volume is now 17-36s depending on system load.

Following the SAME pattern already established by sibling concurrency
tests from this exact fix set
(``test_filesystem_vector_store_1575_round3_gap_c_concurrency.py``):
``WORKER_TIMEOUT_SECONDS`` bounds each individual ``Future.result()`` call
(a plain Python `ThreadPoolExecutor` cannot forcibly kill a hung thread,
so this is a courtesy value, not itself a hard guarantee), while the outer
``pytest.mark.timeout(TEST_TIMEOUT_SECONDS)`` is the actual hard wall-clock
ceiling -- pytest-timeout terminates the test PROCESS outright on a
genuine deadlock, instead of hanging the suite. Both are sized with
generous headroom above the 17-36s observed range so that a real,
reintroduced ABBA deadlock (which hangs forever, independent of any
timeout value chosen here) is still caught reliably, just as before this
change.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 32
COLLECTION = "lock_order_test"
ITERATIONS = 100
INITIAL_FILES = 50
CHUNKS_PER_FILE = 4
NEW_FILE_CHUNK_COUNT = 2
BARRIER_TIMEOUT_SECONDS = 10

# See module docstring: out-of-session upsert/delete calls now legitimately
# cost several synchronous fsync'd writes each (Bug #1575 Gap D/B), pushing
# real wall-clock time for this test to 17-36s. These budgets give ample
# margin over that observed range.
WORKER_TIMEOUT_SECONDS = 60
TEST_TIMEOUT_SECONDS = 90


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

        On the buggy code (reversed lock acquisition order), this test hangs
        indefinitely -- caught by the outer pytest-timeout process-level
        watchdog (see module docstring). On the fixed code it completes
        well within budget.
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
