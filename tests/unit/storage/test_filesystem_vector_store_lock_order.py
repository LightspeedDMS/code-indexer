"""Concurrency regression test for ABBA deadlock between _path_index_lock and _id_index_lock.

BLOCKER B1: upsert_points acquires _path_index_lock (outer) then _id_index_lock (inner).
            delete_points acquires _id_index_lock (outer) then _path_index_lock (inner).
            Running both simultaneously can cause deadlock.

This test spawns two threads — one upserts, one deletes — and asserts both
complete within 30 seconds.  On the buggy code this test HANGS (deadlock).
On the fixed code (uniform lock order) it completes in single-digit seconds.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

import numpy as np

from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 32
COLLECTION = "lock_order_test"
ITERATIONS = 100
INITIAL_FILES = 50
CHUNKS_PER_FILE = 4


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


class TestConcurrentUpsertAndDeleteNoDeadlock:
    """Deadlock regression: upsert_points and delete_points must not ABBA deadlock."""

    def test_concurrent_upsert_and_delete_no_deadlock(self, tmp_path: Path) -> None:
        """Two threads running upsert_points and delete_points concurrently
        must both complete within 30 seconds.

        On the buggy code (reversed lock acquisition order), this test hangs
        indefinitely. On the fixed code it completes in single-digit seconds.
        """
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)

        # Populate initial data
        file_to_ids = _populate_initial(store)
        initial_ids: List[str] = [pid for ids in file_to_ids.values() for pid in ids]

        # Barrier ensures both threads enter their hot loop at the same time,
        # maximising the chance of interleaving that triggers the deadlock.
        barrier = threading.Barrier(2)

        errors: List[str] = []

        def thread_upsert() -> None:
            """Loop upsert_points ITERATIONS times with fresh files."""
            try:
                barrier.wait(timeout=10)
                for k in range(ITERATIONS):
                    fp = f"src/new_file_{uuid.uuid4().hex[:8]}.py"
                    pts = [_make_point(fp, j, f"new_{k}_{j}") for j in range(2)]
                    store.upsert_points(COLLECTION, pts)
            except Exception as exc:
                errors.append(f"upsert thread: {exc}")

        def thread_delete() -> None:
            """Loop delete_points ITERATIONS times on the initial population."""
            try:
                barrier.wait(timeout=10)
                # Work through the initial ids in batches of CHUNKS_PER_FILE
                idx = 0
                for k in range(ITERATIONS):
                    batch = initial_ids[idx : idx + CHUNKS_PER_FILE]
                    if not batch:
                        break
                    store.delete_points(COLLECTION, batch)
                    idx += CHUNKS_PER_FILE
            except Exception as exc:
                errors.append(f"delete thread: {exc}")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(thread_upsert),
                executor.submit(thread_delete),
            ]
            # 30-second timeout — if either future hangs past that, it's a deadlock
            for future in as_completed(futures, timeout=30):
                exc = future.exception()
                if exc is not None:
                    errors.append(str(exc))

        assert not errors, f"Thread errors detected: {errors}"
