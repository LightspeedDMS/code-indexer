"""GitHub Bug #1775 round 5: GENUINE multi-OS-process reproduction.

This is the actual acceptance evidence for round 5. Live staging
validation found: single-worker solo staging passed cleanly, but
2-worker CLUSTERED staging FAILED (fd count grew monotonically across 10
real refresh+query cycles, 11 leaked snapshot generations, 120 follow-up
queries reclaimed zero handles). Root cause: ``ChunkStoreThreadCache``'s
stale-prefix registry (rounds 1-4) lives in per-PROCESS memory -- a
single-process/multi-thread test structurally cannot reproduce this gap,
because "per-process" and "per-server" collapse to the same thing when
there is only one process. This test does NOT make that mistake: it uses
``multiprocessing.get_context("spawn")`` -- a FULL FRESH interpreter per
child, no fork-inherited memory of any kind -- so it can never
accidentally pass via shared Python-level state.

Two real, separate OS processes each construct their OWN
``ChunkStoreThreadCache`` and their OWN ``PayloadCache`` Python object,
both pointed at the SAME real SQLite db file (WAL mode -- the exact
mechanism that makes solo-mode multi-worker deployments safe, and the
same mechanism PostgreSQL-backed cluster mode uses transparently through
the identical public PayloadCache API). Process B opens a real cached
handle for a real snapshot and starts its own real
``ChunkStoreCrossProcessPoller``; Process A -- an entirely independent
process, sharing nothing in RAM with B -- publishes a stale prefix for
that exact snapshot. The test asserts Process B's own independently
cached handle converges to REAL eviction (closed sqlite3 connection,
``ProgrammingError`` on the next real read) within a bounded deadline.
"""

import multiprocessing
import sqlite3
import time
from pathlib import Path
from typing import Optional

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.shared.chunk_store_cache_cross_process import (
    ChunkStoreCrossProcessPoller,
    publish_stale_prefix,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
POINT_ID = "p1"
FAST_TTL_SECONDS = 900
FAST_CLEANUP_INTERVAL_SECONDS = 60
CHILD_POLL_INTERVAL_SECONDS = 0.2
CHILD_CONVERGENCE_WAIT_SECONDS = 8.0
CHILD_CONVERGENCE_POLL_STEP_SECONDS = 0.1
CHILD_READY_TIMEOUT_SECONDS = 15.0
CHILD_JOIN_TIMEOUT_SECONDS = 30.0
CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS = 5.0
RESULT_QUEUE_GET_TIMEOUT_SECONDS = 5.0


def _make_payload_cache(db_path_str: str) -> PayloadCache:
    config = PayloadCacheConfig(
        cache_ttl_seconds=FAST_TTL_SECONDS,
        cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=Path(db_path_str), config=config)
    cache.initialize()
    return cache


def _make_versioned_snapshot(base: Path) -> tuple:
    snapshot_dir = base / ".versioned" / "repo" / "v_1"
    collection_dir = snapshot_dir / ".code-indexer" / "index" / PROVIDER_DIR
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [
                {
                    "id": POINT_ID,
                    "vector": VECTOR,
                    "payload": {"path": f"{POINT_ID}.py"},
                }
            ]
        )
    finally:
        store.close()
    return str(db_path), str(collection_dir), str(snapshot_dir)


def _worker_a_publish(payload_db_path_str, v1_dir, ready_event, result_queue):
    """Process A: a REAL, separate OS process. Simulates the worker that
    performed a refresh/alias-swap -- publishes the stale prefix to the
    shared cross-process registry. Waits for Process B's readiness
    signal first, reproducing the real production race shape (a worker
    caches a handle, THEN a DIFFERENT worker performs the refresh some
    time later).
    """
    payload_cache: Optional[PayloadCache] = None
    try:
        if not ready_event.wait(timeout=CHILD_READY_TIMEOUT_SECONDS):
            result_queue.put(
                {"role": "A", "ok": False, "error": "B never signaled ready"}
            )
            return

        payload_cache = _make_payload_cache(payload_db_path_str)
        publish_stale_prefix(payload_cache, v1_dir)
        result_queue.put({"role": "A", "ok": True})
    except Exception as exc:
        result_queue.put(
            {"role": "A", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        if payload_cache is not None:
            payload_cache.close()


def _wait_for_convergence(chunk_store_cache, v1_db, v1_coll, store_first) -> bool:
    """Poll via repeated same-thread get_or_open() calls (the owning
    thread's own continued query activity) until it returns a genuinely
    different object than store_first, or the deadline elapses.
    """
    deadline = time.monotonic() + CHILD_CONVERGENCE_WAIT_SECONDS
    while time.monotonic() < deadline:
        current_store = chunk_store_cache.get_or_open(v1_db, v1_coll)
        if current_store is not store_first:
            return True
        time.sleep(CHILD_CONVERGENCE_POLL_STEP_SECONDS)
    return False


def _assert_store_first_closed(store_first) -> Optional[str]:
    """Returns None if store_first is genuinely closed (real proof, no
    mocking), else an error string describing the failure.
    """
    try:
        store_first.read(POINT_ID)
        return "store_first still readable after convergence"
    except sqlite3.ProgrammingError:
        return None


def _worker_b_converge(payload_db_path_str, v1_db, v1_coll, ready_event, result_queue):
    """Process B: a REAL, separate OS process, entirely independent from
    Process A -- its own interpreter, its own ``ChunkStoreThreadCache``
    module-level singleton state, its own ``PayloadCache`` Python object
    (pointed at the SAME real db file). Opens its own handle for the
    SAME snapshot (mimicking a second worker independently caching the
    same collection -- the real production topology), starts its own
    ``ChunkStoreCrossProcessPoller``, signals readiness, then must
    converge to evicting that handle purely from Process A's publish.
    """
    chunk_store_cache = ChunkStoreThreadCache()
    payload_cache: Optional[PayloadCache] = None
    poller: Optional[ChunkStoreCrossProcessPoller] = None
    try:
        payload_cache = _make_payload_cache(payload_db_path_str)

        store_first = chunk_store_cache.get_or_open(v1_db, v1_coll)
        if store_first.read(POINT_ID) is None:
            result_queue.put(
                {"role": "B", "ok": False, "error": "initial read returned None"}
            )
            return

        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=chunk_store_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=CHILD_POLL_INTERVAL_SECONDS,
        )
        poller.start()
        ready_event.set()

        if not _wait_for_convergence(chunk_store_cache, v1_db, v1_coll, store_first):
            result_queue.put(
                {"role": "B", "ok": False, "error": "did not converge within deadline"}
            )
            return

        closure_error = _assert_store_first_closed(store_first)
        if closure_error is not None:
            result_queue.put({"role": "B", "ok": False, "error": closure_error})
            return

        result_queue.put({"role": "B", "ok": True})
    except Exception as exc:
        result_queue.put(
            {"role": "B", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        if poller is not None:
            poller.stop()
        chunk_store_cache.close_current_thread()
        if payload_cache is not None:
            payload_cache.close()


def _terminate_stray_processes(*processes) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS)


class TestGenuineMultiProcessConvergence:
    def test_worker_b_converges_to_evicting_a_prefix_published_by_worker_a(
        self, tmp_path
    ):
        payload_db_path = tmp_path / "payload_cache.db"
        # Pre-create the PayloadCache schema from the PARENT process so
        # both children see a ready schema regardless of spawn-race
        # ordering.
        _make_payload_cache(str(payload_db_path)).close()

        v1_db, v1_coll, v1_dir = _make_versioned_snapshot(tmp_path)

        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        ready_event = ctx.Event()

        process_b = ctx.Process(
            target=_worker_b_converge,
            args=(str(payload_db_path), v1_db, v1_coll, ready_event, result_queue),
        )
        process_a = ctx.Process(
            target=_worker_a_publish,
            args=(str(payload_db_path), v1_dir, ready_event, result_queue),
        )

        process_b.start()
        process_a.start()
        try:
            process_a.join(timeout=CHILD_JOIN_TIMEOUT_SECONDS)
            process_b.join(timeout=CHILD_JOIN_TIMEOUT_SECONDS)

            assert not process_a.is_alive(), (
                "Process A (publisher) did not finish in time"
            )
            assert not process_b.is_alive(), "Process B (poller) did not finish in time"

            # Round-6 test nit (Codex): Queue.empty() is documented as
            # unreliable around feeder-thread flushing -- get() exactly
            # twice instead (both processes already joined above, so
            # both results are either already enqueued or arriving
            # imminently).
            results = [
                result_queue.get(timeout=RESULT_QUEUE_GET_TIMEOUT_SECONDS)
                for _ in range(2)
            ]
            for result in results:
                assert result["ok"], (
                    f"Process {result['role']} reported failure: {result}"
                )
        finally:
            _terminate_stray_processes(process_a, process_b)
