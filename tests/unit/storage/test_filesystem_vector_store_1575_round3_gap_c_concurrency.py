"""Bug #1575 PathIndex-shortcut mechanism -- THIRD dual-review round, Gap C
(Codex, reproduced): "a real concurrency race."

The CHUNKS_DB ``delete_points()`` branch used to commit the SQLite mutation
BEFORE acquiring ``_path_index_lock`` to update the in-memory PathIndex --
so a concurrent ``upsert_points()`` call for the SAME point, interleaved
between those two steps, could leave the database and the PathIndex cache
disagreeing (empirically reproduced via a throwaway probe script mirroring
this exact pre-fix ordering: 284/300 trials disagreed).

Fix C acquires ``_path_index_lock`` BEFORE ``chunk_store.delete()`` and
holds it through the in-memory PathIndex removal, so delete's own
{DB-commit, cache-update} pair becomes one indivisible critical section --
no other thread holding the same lock (e.g. ``upsert_points()``'s own
cache-mutation critical section) can ever observe it half-done. The SAME
probe methodology against the fixed code showed 0/300 disagreements.

This test uses REAL threads (via ``ThreadPoolExecutor``, no mocking of the
code under test) driving the actual production
``FilesystemVectorStore.delete_points()``/``upsert_points()`` methods, and
asserts the chunk store and the live PathIndex cache never disagree about a
contended point's existence across many real concurrent trials. Worker
exceptions are propagated via ``Future.result()`` so a raise inside either
thread fails the test loudly instead of being silently swallowed. A
``pytest.mark.timeout`` marker (pytest-timeout, already installed in this
repo) provides a hard wall-clock backstop for the WHOLE test process: a
``Future.result(timeout=...)`` alone cannot save this test from hanging on
a genuine deadlock, because the enclosing ``ThreadPoolExecutor`` context
manager still calls ``shutdown(wait=True)`` on exit, which blocks
indefinitely on a truly stuck worker -- the marker's watchdog fails the
test outright instead.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import open_chunk_store_for_path

from _pathindex_gap_1575_helpers import make_vector

CONTENDED_POINT_ID = "contended_point"
CONTENDED_FILE_PATH = "src/contended.py"
CONCURRENCY_TRIALS = 100
VECTOR_SIZE = 8
INITIAL_SEED = 1
WORKER_COUNT = 2
# Per-call ceiling for a real (non-mocked) delete/upsert call under
# test-suite load -- exists only to fail loudly on a genuine deadlock
# rather than hang the suite; not tuned to any specific environment's real
# runtime, which is milliseconds. The test-level marker below is the real
# backstop (see module docstring): a Future timing out here still leaves
# ThreadPoolExecutor's own shutdown(wait=True) to block indefinitely on a
# truly stuck worker.
WORKER_TIMEOUT_SECONDS = 30
# Hard wall-clock ceiling for the ENTIRE test (all CONCURRENCY_TRIALS
# iterations), enforced by pytest-timeout's watchdog -- terminates the test
# process outright on a genuine deadlock instead of hanging the suite.
TEST_TIMEOUT_SECONDS = 60


def _make_contended_point(seed: int) -> Dict[str, Any]:
    return {
        "id": CONTENDED_POINT_ID,
        "vector": make_vector(seed),
        "payload": {
            "path": CONTENDED_FILE_PATH,
            "type": "content",
            "hidden_branches": [],
        },
    }


def _build_single_point_chunks_db_store(tmp_path: Path):
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    collection_name = "coll"
    store.create_collection(collection_name, vector_size=VECTOR_SIZE)
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, [_make_contended_point(seed=INITIAL_SEED)])
    store.end_indexing(collection_name)
    return store, collection_name


def _db_has_contended_point(collection_path: Path) -> bool:
    chunk_store = open_chunk_store_for_path(
        collection_path / "chunks.db", str(collection_path)
    )
    try:
        return (
            chunk_store.get_paths_for_points([CONTENDED_POINT_ID]).get(
                CONTENDED_POINT_ID
            )
            is not None
        )
    finally:
        chunk_store.close()


def _cache_has_contended_point(
    store: FilesystemVectorStore, collection_name: str
) -> bool:
    path_index = store._path_indexes[collection_name]
    return CONTENDED_POINT_ID in path_index.get_point_ids(CONTENDED_FILE_PATH)


def _run_one_concurrent_delete_and_upsert_trial(
    store: FilesystemVectorStore,
    collection_name: str,
    seed: int,
    barrier: threading.Barrier,
) -> None:
    def do_delete() -> None:
        barrier.wait()
        store.delete_points(collection_name, [CONTENDED_POINT_ID])

    def do_upsert() -> None:
        barrier.wait()
        store.upsert_points(collection_name, [_make_contended_point(seed=seed)])

    # ThreadPoolExecutor + Future.result() propagates a worker exception to
    # the calling (test) thread -- a bare Thread.join() would let a raise
    # inside either worker pass silently, so the test could pass on
    # whatever stale db/cache state existed BEFORE the crash.
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
        delete_future = executor.submit(do_delete)
        upsert_future = executor.submit(do_upsert)
        delete_future.result(timeout=WORKER_TIMEOUT_SECONDS)
        upsert_future.result(timeout=WORKER_TIMEOUT_SECONDS)


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_gap_c_concurrent_delete_and_upsert_never_disagree(tmp_path):
    """Real threads, real SQLite, real FilesystemVectorStore: hammer a
    concurrent delete_points()/upsert_points() race on the SAME point many
    times and assert the chunk store and the live PathIndex cache are
    NEVER caught disagreeing about whether that point exists."""
    store, collection_name = _build_single_point_chunks_db_store(tmp_path)
    collection_path = tmp_path / collection_name
    barrier = threading.Barrier(WORKER_COUNT)

    disagreements = []
    for trial in range(CONCURRENCY_TRIALS):
        barrier.reset()
        _run_one_concurrent_delete_and_upsert_trial(
            store, collection_name, seed=trial, barrier=barrier
        )
        db_has = _db_has_contended_point(collection_path)
        cache_has = _cache_has_contended_point(store, collection_name)
        if db_has != cache_has:
            disagreements.append((trial, db_has, cache_has))

    assert disagreements == [], (
        f"expected ZERO disagreements between the chunk store and the live "
        f"PathIndex cache across {CONCURRENCY_TRIALS} concurrent "
        f"delete_points()/upsert_points() trials on the same point -- got "
        f"{len(disagreements)}: {disagreements[:5]}. Fix C requires "
        f"delete_points()'s CHUNKS_DB branch to hold _path_index_lock "
        f"across BOTH the SQLite delete commit and the in-memory PathIndex "
        f"removal so no concurrent upsert_points() call can observe (or "
        f"produce) a torn state."
    )
