"""Concurrency safety proof for the shared parallel-query executor (Issue #1516).

Mandatory safety constraint from the issue: sharing a `ThreadPoolExecutor`
across requests must not violate sqlite3's same-thread contract (Story
#1456 AC7 -- "sqlite3 connections are NOT safely shared across threads").

Reasoning under test: a `ThreadPoolExecutor` worker thread processes
exactly ONE submitted task at a time (never two tasks concurrently on the
same worker). So if `ChunkStoreThreadCache.get_or_open()` is called from
within tasks submitted to the shared pool, each worker thread's
`threading.local()` slot is only ever touched by that one worker, one task
at a time -- no cross-thread sqlite3 connection sharing can occur.

This test uses REAL threads (the actual shared executor singleton), REAL
sqlite3 (`ChunkStore` against a real on-disk `chunks.db`), and DETERMINISTIC
genuine concurrency: a `threading.Barrier(EXPECTED_MAX_WORKERS)` forces
BOTH worker threads to be simultaneously active before any cache access
happens -- a barrier of N cannot release until all N parties are blocked
on it, so this is a guarantee, not a "fast tasks probably overlap" hope.
No mocking of the storage layer.
"""

import threading
from pathlib import Path
from typing import List, Tuple, cast

import numpy as np
import pytest

from code_indexer.server.query.parallel_query_executor import (
    get_global_parallel_query_executor,
    reset_global_parallel_query_executor,
)
from code_indexer.storage.shared.chunk_store_cache import (
    ChunkStoreThreadCache,
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 8
RECORD_COUNT = 6
# Must match parallel_query_executor.py's configured worker cap exactly.
EXPECTED_MAX_WORKERS = 2
# Additional (non-barrier-gated) submissions after the workers are warmed up.
FOLLOWUP_TASK_COUNT = 12
TASK_TIMEOUT_SECONDS = 10


@pytest.fixture(autouse=True)
def reset_singletons():
    reset_global_parallel_query_executor()
    reset_global_chunk_store_cache()
    yield
    reset_global_parallel_query_executor()
    reset_global_chunk_store_cache()


def _make_vector(seed: int) -> List[float]:
    rng = np.random.RandomState(seed)
    return cast(List[float], rng.rand(VECTOR_DIM).astype(np.float32).tolist())


def _build_real_chunks_db(db_path: Path) -> dict:
    """Write RECORD_COUNT real chunk records to a real chunks.db file."""
    expected: dict = {}
    store = ChunkStore(db_path)
    try:
        records = []
        for i in range(RECORD_COUNT):
            point_id = f"point-{i}"
            vector = _make_vector(seed=i)
            expected[point_id] = vector
            records.append(
                {
                    "id": point_id,
                    "vector": vector,
                    "payload": {"path": f"src/file_{i}.py"},
                    "chunk_text": f"content for {point_id}",
                }
            )
        store.write_batch(records)
    finally:
        store.close()
    return expected


def _setup_real_store(tmp_path) -> Tuple[Path, str, dict, list]:
    db_path = tmp_path / "chunks.db"
    collection_path = str(tmp_path)
    expected_vectors = _build_real_chunks_db(db_path)
    return db_path, collection_path, expected_vectors, list(expected_vectors.keys())


def _warm_up_and_assert_both_workers_active(executor, barrier_task_fn) -> None:
    """Force EXPECTED_MAX_WORKERS worker threads simultaneously active.

    A barrier of N parties cannot release ANY waiter until all N are
    blocked on it -- so a successful return here is a deterministic proof
    that EXPECTED_MAX_WORKERS distinct worker threads were genuinely
    running concurrently, never an incidental/probabilistic observation.
    """
    barrier = threading.Barrier(EXPECTED_MAX_WORKERS, timeout=TASK_TIMEOUT_SECONDS)
    futures = [
        executor.submit(barrier_task_fn, barrier) for _ in range(EXPECTED_MAX_WORKERS)
    ]
    thread_ids = {f.result(timeout=TASK_TIMEOUT_SECONDS) for f in futures}
    assert len(thread_ids) == EXPECTED_MAX_WORKERS, (
        f"Expected {EXPECTED_MAX_WORKERS} distinct worker threads to be "
        f"genuinely concurrently active (barrier-proven), got "
        f"{len(thread_ids)}: {thread_ids}"
    )


def _assert_reads_correct(results: list, expected_vectors: dict) -> None:
    for point_id, record in results:
        assert record is not None, (
            f"Expected a real record for {point_id!r}, got None -- read "
            "failed or was served a stale/wrong-thread connection"
        )
        np.testing.assert_allclose(
            record["vector"],
            expected_vectors[point_id],
            rtol=1e-5,
            err_msg=(
                f"Vector mismatch for {point_id!r}: cross-talk/corruption "
                "between concurrent tasks sharing the executor."
            ),
        )
        assert record["payload"]["path"].endswith(".py")
        assert record["chunk_text"] == f"content for {point_id}"


def test_concurrent_reads_via_shared_cache_are_correct_under_real_concurrency(
    tmp_path,
):
    db_path, collection_path, expected_vectors, point_ids = _setup_real_store(tmp_path)
    executor = get_global_parallel_query_executor()
    cache: ChunkStoreThreadCache = get_global_chunk_store_cache()
    results_lock = threading.Lock()
    results: list = []

    def _read_and_record(point_id: str) -> None:
        store = cache.get_or_open(db_path, collection_path)
        record = store.read(point_id)
        with results_lock:
            results.append((point_id, record))

    def _barrier_gated_read(barrier) -> int:
        barrier.wait(timeout=TASK_TIMEOUT_SECONDS)
        _read_and_record(point_ids[0])
        return threading.get_ident()

    _warm_up_and_assert_both_workers_active(executor, _barrier_gated_read)

    followup_futures = [
        executor.submit(_read_and_record, point_ids[i % len(point_ids)])
        for i in range(FOLLOWUP_TASK_COUNT)
    ]
    for f in followup_futures:
        f.result(timeout=TASK_TIMEOUT_SECONDS)

    assert len(results) == EXPECTED_MAX_WORKERS + FOLLOWUP_TASK_COUNT
    _assert_reads_correct(results, expected_vectors)


def test_distinct_worker_threads_never_share_a_chunk_store_object(tmp_path):
    """Each worker thread must observe exactly one, thread-exclusive
    ChunkStore object -- the barrier-gated warmup guarantees >=2 distinct
    threads participate, so this is a genuine, non-vacuous proof."""
    db_path, collection_path, expected_vectors, point_ids = _setup_real_store(tmp_path)
    point_id = point_ids[0]
    executor = get_global_parallel_query_executor()
    cache: ChunkStoreThreadCache = get_global_chunk_store_cache()
    lock = threading.Lock()
    observations: list = []

    def _record_observation() -> int:
        store = cache.get_or_open(db_path, collection_path)
        record = store.read(point_id)
        tid = threading.get_ident()
        with lock:
            observations.append((tid, id(store), record is not None))
        return tid

    def _barrier_gated_observe(barrier) -> int:
        barrier.wait(timeout=TASK_TIMEOUT_SECONDS)
        return _record_observation()

    _warm_up_and_assert_both_workers_active(executor, _barrier_gated_observe)

    followup_futures = [
        executor.submit(_record_observation) for _ in range(FOLLOWUP_TASK_COUNT)
    ]
    for f in followup_futures:
        f.result(timeout=TASK_TIMEOUT_SECONDS)

    assert len(observations) == EXPECTED_MAX_WORKERS + FOLLOWUP_TASK_COUNT
    assert all(found for _tid, _store_id, found in observations)

    by_thread: dict = {}
    for tid, store_id, _found in observations:
        by_thread.setdefault(tid, set()).add(store_id)

    assert len(by_thread) >= EXPECTED_MAX_WORKERS
    for tid, store_ids in by_thread.items():
        assert len(store_ids) == 1, (
            f"Thread {tid} observed multiple distinct ChunkStore object ids "
            f"{store_ids} for the same db_path -- expected exactly one "
            "cached handle per thread."
        )

    flattened = [sid for ids in by_thread.values() for sid in ids]
    assert len(flattened) == len(set(flattened)), (
        "Distinct worker threads must never share the identical ChunkStore "
        "object -- that would indicate a cross-thread sqlite3 connection "
        "sharing violation."
    )
