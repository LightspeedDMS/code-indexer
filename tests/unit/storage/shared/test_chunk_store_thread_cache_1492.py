"""Story #1492 AC3: ChunkStore is not reopened (with schema DDL) on every
query for mutable collections.

Finding C5 (MEDIUM, report rank 11): open_chunk_store_for_path() was called
per query, and for mutable collections ChunkStore.__init__ runs
_ensure_schema() DDL + _load_persisted_dim() + constructs fresh zstd codec
objects EVERY time. ChunkStoreThreadCache introduces a per-collection
cache/pool that:

- Reuses the SAME ChunkStore object (and its open sqlite3 connection) on a
  repeat open from the SAME thread against an unchanged chunks.db (mtime
  identical). Since ChunkStore.__init__ is the ONLY call site of
  _ensure_schema()/_load_persisted_dim(), object-identity reuse (asserted
  below) IS the proof that no repeat DDL/dim-load/codec-construction ever
  happens -- no monkeypatching of the storage layer under test is needed.
- MANDATORY sqlite3 cross-thread contract (Story #1456): sqlite3
  connections cannot be safely shared across threads. This cache is
  built on threading.local() -- two DIFFERENT threads opening the SAME
  db_path always get DISTINCT ChunkStore objects/connections, never a
  shared one. Proven with REAL threads (concurrent.futures.ThreadPoolExecutor
  + a threading.Barrier that forces genuinely concurrent, distinct worker
  threads rather than relying on pool scheduling), not mocks.
- Detects a genuinely changed chunks.db (a fresh file replacing the old
  one, e.g. via os.replace during a rebuild) via mtime and reopens rather
  than serving a stale cached handle.

Real SQLite files, real ChunkStore, real threads -- no mocking of the
storage layer under test. Every ChunkStore obtained from the cache is
closed from ITS OWNING THREAD (threading.local semantics) in a finally
block.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Tuple

import pytest

from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
NUM_WORKER_THREADS = 4
MTIME_SEPARATION_DELAY_SECONDS = 0.01


def _write_one_record(db_path: Path, point_id: str) -> None:
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()


@pytest.fixture
def cache():
    c = ChunkStoreThreadCache()
    yield c
    c.close_current_thread()


class TestReuseWithinSameThread:
    def test_repeat_get_or_open_same_thread_reuses_same_instance(self, tmp_path, cache):
        db_path = tmp_path / "chunks.db"
        _write_one_record(db_path, "p1")

        store1 = cache.get_or_open(db_path, str(tmp_path))
        store2 = cache.get_or_open(db_path, str(tmp_path))

        # Same object -- proves the underlying connection (and its schema,
        # already created once inside ChunkStore.__init__) is reused, never
        # reopened/re-initialized: ChunkStore.__init__ is the ONLY call
        # site of _ensure_schema()/_load_persisted_dim(), so object-identity
        # reuse is direct evidence no repeat DDL ever ran.
        assert store1 is store2
        assert store1.read("p1") is not None


class TestMtimeInvalidation:
    def test_changed_chunks_db_is_not_served_from_stale_cache(self, tmp_path, cache):
        db_path = tmp_path / "chunks.db"
        _write_one_record(db_path, "old_point")

        store1 = cache.get_or_open(db_path, str(tmp_path))
        assert store1.read("old_point") is not None
        assert store1.read("new_point") is None

        # Simulate a rebuild that replaces chunks.db with fresh content at
        # the SAME path (a new file -> new mtime).
        time.sleep(MTIME_SEPARATION_DELAY_SECONDS)
        tmp_db = tmp_path / "chunks_new.db"
        _write_one_record(tmp_db, "new_point")
        os.replace(tmp_db, db_path)

        store2 = cache.get_or_open(db_path, str(tmp_path))
        # The cached handle must NOT be reused to serve stale data.
        assert store2.read("new_point") is not None
        assert store2.read("old_point") is None


# --- Cross-thread safety: helpers factored out to keep the test itself
# short (setup / concurrent execution / assertion, each isolated). ---

_ResultsMap = Dict[str, Tuple[int, int, bool]]


def _make_worker(cache, db_path, collection_path_str, results, results_lock, barrier):
    def worker(thread_key: str) -> None:
        barrier.wait()
        try:
            store = cache.get_or_open(db_path, collection_path_str)
            # Actually USE the connection from this thread (a real query),
            # proving no cross-thread sqlite3 "created in a different
            # thread" exception occurs.
            record = store.read("p1")
            with results_lock:
                results[thread_key] = (
                    threading.get_ident(),
                    id(store),
                    record is not None,
                )
        finally:
            # Close THIS thread's cache entry from ITS OWN thread
            # (threading.local semantics) -- never from a different thread.
            cache.close_current_thread()

    return worker


def _run_workers_concurrently(worker) -> None:
    with ThreadPoolExecutor(max_workers=NUM_WORKER_THREADS) as executor:
        futures = [executor.submit(worker, f"t{i}") for i in range(NUM_WORKER_THREADS)]
        for f in futures:
            f.result()  # re-raises any exception from the worker thread


def _assert_every_thread_got_a_distinct_connection(results: "_ResultsMap") -> None:
    assert len(results) == NUM_WORKER_THREADS
    assert all(ok for _tid, _obj_id, ok in results.values())

    thread_ids = {tid for tid, _obj_id, _ok in results.values()}
    assert len(thread_ids) == NUM_WORKER_THREADS, (
        "test setup invariant violated: expected "
        f"{NUM_WORKER_THREADS} distinct OS threads, got {len(thread_ids)}"
    )

    # Every DISTINCT thread got a DISTINCT ChunkStore object/connection --
    # never a shared one across threads.
    object_ids = {obj_id for _tid, obj_id, _ok in results.values()}
    assert len(object_ids) == NUM_WORKER_THREADS


class TestCrossThreadSafety:
    def test_different_threads_never_share_the_same_connection(self, tmp_path, cache):
        db_path = tmp_path / "chunks.db"
        _write_one_record(db_path, "p1")

        results: "_ResultsMap" = {}
        results_lock = threading.Lock()
        # Forces all NUM_WORKER_THREADS tasks to be genuinely in-flight at
        # once (blocked here until every one has started), so the pool
        # cannot satisfy the workload by reusing fewer than
        # NUM_WORKER_THREADS distinct OS threads.
        barrier = threading.Barrier(NUM_WORKER_THREADS)

        worker = _make_worker(
            cache, db_path, str(tmp_path), results, results_lock, barrier
        )
        _run_workers_concurrently(worker)
        _assert_every_thread_got_a_distinct_connection(results)


class TestGlobalChunkStoreCacheSingleton:
    """Post-manual-E2E-test production fix (Story #1492 follow-up).

    A real running server was strace-verified to show ZERO cross-request
    benefit from ChunkStoreThreadCache: every query constructs a fresh
    FilesystemVectorStore, and FilesystemVectorStore.__init__ only builds a
    ChunkStoreThreadCache() when the caller passes None -- so every
    instance got its own private, single-use cache. Since the module's own
    docstring states one shared instance is safe across as many threads as
    needed (it only coordinates which per-thread store lives under
    threading.local()), get_global_chunk_store_cache() is the process-wide
    singleton getter FilesystemBackend.get_vector_store_client() must
    inject in server mode, mirroring get_global_id_index_cache().
    """

    def setup_method(self) -> None:
        from code_indexer.storage.shared.chunk_store_cache import (
            reset_global_chunk_store_cache,
        )

        reset_global_chunk_store_cache()

    def teardown_method(self) -> None:
        from code_indexer.storage.shared.chunk_store_cache import (
            reset_global_chunk_store_cache,
        )

        reset_global_chunk_store_cache()

    def test_returns_same_instance_across_calls(self) -> None:
        from code_indexer.storage.shared.chunk_store_cache import (
            get_global_chunk_store_cache,
        )

        first = get_global_chunk_store_cache()
        second = get_global_chunk_store_cache()
        assert first is second

    def test_returns_a_real_chunk_store_thread_cache_instance(self) -> None:
        from code_indexer.storage.shared.chunk_store_cache import (
            get_global_chunk_store_cache,
        )

        instance = get_global_chunk_store_cache()
        assert isinstance(instance, ChunkStoreThreadCache)

    def test_reset_creates_a_fresh_instance(self) -> None:
        from code_indexer.storage.shared.chunk_store_cache import (
            get_global_chunk_store_cache,
            reset_global_chunk_store_cache,
        )

        first = get_global_chunk_store_cache()
        reset_global_chunk_store_cache()
        second = get_global_chunk_store_cache()
        assert first is not second
