"""GitHub Bug #1775 round 5: ``ChunkStoreCrossProcessPoller`` single-
process behavior tests.

Real ``ChunkStoreThreadCache`` + real SQLite-backed ``PayloadCache`` --
no mocking. Proves ``poll_once()`` correctly discovers a prefix published
via ``publish_stale_prefix()`` and feeds it into the SAME, already-proven
(rounds 1-4) ``ChunkStoreThreadCache.invalidate_prefix()`` pipeline, using
the same real-fd-closure evidence style this whole test suite establishes
(a closed sqlite3 connection raises ``ProgrammingError`` on the next real
operation).

See the sibling ``test_chunk_store_cache_cross_process_multiproc_1775.py``
for the genuine multi-OS-process reproduction that is the actual
acceptance evidence for this bug -- this file is single-process coverage
of the poller mechanics in isolation.
"""

import sqlite3
import threading
import time

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.shared.chunk_store_cache_cross_process import (
    ChunkStoreCrossProcessPoller,
    publish_stale_prefix,
)
from tests.unit.storage.shared.test_chunk_store_cache_stale_prefix_eviction_1775 import (
    make_versioned_snapshot,
)

FAST_TTL_SECONDS = 900
FAST_CLEANUP_INTERVAL_SECONDS = 60
SHORT_POLL_INTERVAL_SECONDS = 0.1
CONVERGENCE_WAIT_SECONDS = 2.0
CONVERGENCE_POLL_STEP_SECONDS = 0.05


class _CountingChunkStoreCache:
    """Minimal test double exposing ONLY the ``invalidate_prefix()``
    surface ``ChunkStoreCrossProcessPoller`` actually calls -- used
    solely to prove exact call counts for the poller's OWN "already
    applied" dedup bookkeeping, which the real ``ChunkStoreThreadCache``
    cannot discriminate here (its per-key ``_is_stale()`` check would
    keep flagging a re-accessed key as stale regardless of whether the
    poller redundantly re-invokes ``invalidate_prefix()``).
    """

    def __init__(self) -> None:
        self.calls: list = []

    def invalidate_prefix(self, prefix: str) -> None:
        self.calls.append(prefix)


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    config = PayloadCacheConfig(
        cache_ttl_seconds=FAST_TTL_SECONDS,
        cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=db_path, config=config)
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def chunk_store_cache():
    c = ChunkStoreThreadCache()
    yield c
    c.close_current_thread()


class TestPollOnceAppliesPublishedPrefix:
    def test_poll_once_evicts_local_entry_for_a_prefix_published_by_publish(
        self, tmp_path, payload_cache, chunk_store_cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        store_first = chunk_store_cache.get_or_open(v1_db, v1_coll)
        assert store_first.read("p1") is not None

        # Simulates ANOTHER process publishing the invalidation -- this
        # process's own local invalidate_prefix() is deliberately NEVER
        # called directly; only the cross-process registry is written.
        publish_stale_prefix(payload_cache, v1_dir)

        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=chunk_store_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=SHORT_POLL_INTERVAL_SECONDS,
        )
        applied_count = poller.poll_once()
        assert applied_count == 1

        # Round 1-4 established design: invalidate_prefix() only
        # REGISTERS the stale prefix (safe from any thread); actual
        # LOCAL eviction happens lazily on THIS (owning) thread's next
        # get_or_open() call, never spontaneously from a different
        # thread's registration alone (sqlite3 thread-affinity
        # contract). This same-thread call is what triggers it.
        store_second = chunk_store_cache.get_or_open(v1_db, v1_coll)
        assert store_second is not store_first
        assert store_second.read("p1") is not None

        with pytest.raises(sqlite3.ProgrammingError):
            store_first.read("p1")

    def test_poll_once_with_nothing_new_does_not_redundantly_reinvoke_invalidate(
        self, tmp_path, payload_cache
    ):
        _v1_db, _v1_coll, v1_dir = make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )
        publish_stale_prefix(payload_cache, v1_dir)

        counting_cache = _CountingChunkStoreCache()
        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=counting_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=SHORT_POLL_INTERVAL_SECONDS,
        )
        first_count = poller.poll_once()
        second_count = poller.poll_once()

        assert first_count == 1
        assert second_count == 0
        assert counting_cache.calls == [v1_dir], (
            "invalidate_prefix() must be called EXACTLY once total across "
            "both poll_once() calls -- a second poll with nothing new in "
            "the registry must not redundantly re-invoke it for an "
            "already-known prefix."
        )


SLOW_INVALIDATE_BLOCK_SECONDS = 1.0
SHORT_STOP_JOIN_TIMEOUT_SECONDS = 0.05
SLOW_CACHE_ENTRY_TIMEOUT_SECONDS = 5.0
CLEANUP_JOIN_TIMEOUT_SECONDS = 5.0


class _SlowChunkStoreCache:
    """Test double whose invalidate_prefix() blocks for a configurable
    duration -- simulates a poller genuinely stuck mid-DB-I/O, used to
    prove stop() detects and reports a thread that does NOT exit within
    its join timeout, rather than silently proceeding as if it had.
    Sets ``entered`` the moment invalidate_prefix() begins, so a test
    can deterministically wait for the slow call to actually be
    in-flight before calling stop() -- no sleep-based racing.
    """

    def __init__(self, block_seconds: float) -> None:
        self._block_seconds = block_seconds
        self.entered = threading.Event()

    def invalidate_prefix(self, prefix: str) -> None:
        self.entered.set()
        time.sleep(self._block_seconds)


class TestStopConfirmsThreadExit:
    """Round-6 code review finding (Codex): stop() only waited 2s with
    no proof the poller thread actually exited -- a poller genuinely
    blocked in DB I/O could still be running when the caller (lifespan.
    py) proceeds to close PayloadCache: a real use-after-close race.
    """

    def test_stop_returns_true_when_thread_exits_normally(
        self, tmp_path, payload_cache, chunk_store_cache
    ):
        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=chunk_store_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=SHORT_POLL_INTERVAL_SECONDS,
        )
        poller.start()
        assert poller._thread is not None

        result = poller.stop()

        assert result is True
        assert not poller._thread.is_alive()

    def test_stop_returns_false_and_warns_when_thread_does_not_exit_in_time(
        self, payload_cache, caplog
    ):
        import logging

        slow_cache = _SlowChunkStoreCache(block_seconds=SLOW_INVALIDATE_BLOCK_SECONDS)
        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=slow_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=SHORT_POLL_INTERVAL_SECONDS,
            stop_join_timeout_seconds=SHORT_STOP_JOIN_TIMEOUT_SECONDS,
        )
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")
        poller.start()
        assert poller._thread is not None
        assert slow_cache.entered.wait(timeout=SLOW_CACHE_ENTRY_TIMEOUT_SECONDS), (
            "Test setup: the slow invalidate_prefix() call must actually "
            "be in-flight before stop() is called, or this test proves "
            "nothing."
        )

        with caplog.at_level(logging.WARNING):
            result = poller.stop()

        assert result is False
        assert any(
            "did not stop within" in record.message for record in caplog.records
        ), "stop() must log a WARNING when the thread fails to exit in time."

        # Cleanup: wait out the slow call for real so it doesn't leak
        # into a later test.
        poller._thread.join(timeout=CLEANUP_JOIN_TIMEOUT_SECONDS)
        assert not poller._thread.is_alive(), (
            "Test cleanup: the slow thread must have genuinely finished "
            "by now (block duration is well under the cleanup timeout) "
            "-- a still-alive thread here would leak into later tests."
        )


class TestBackgroundThreadConverges:
    def test_started_poller_converges_within_a_bounded_number_of_intervals(
        self, tmp_path, payload_cache, chunk_store_cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        store_first = chunk_store_cache.get_or_open(v1_db, v1_coll)

        poller = ChunkStoreCrossProcessPoller(
            chunk_store_cache=chunk_store_cache,
            payload_cache=payload_cache,
            poll_interval_seconds=SHORT_POLL_INTERVAL_SECONDS,
        )
        poller.start()
        try:
            publish_stale_prefix(payload_cache, v1_dir)

            # The background poller thread can only REGISTER the prefix
            # in the shared registry; actual LOCAL eviction still
            # requires THIS (owning) thread to call get_or_open() again
            # -- so this loop simulates the owning thread's own
            # continued query activity, exactly like a real worker
            # thread handling subsequent requests.
            deadline = time.monotonic() + CONVERGENCE_WAIT_SECONDS
            converged = False
            while time.monotonic() < deadline:
                current_store = chunk_store_cache.get_or_open(v1_db, v1_coll)
                if current_store is not store_first:
                    converged = True
                    break
                time.sleep(CONVERGENCE_POLL_STEP_SECONDS)

            assert converged, (
                f"Background poller must close the stale local handle "
                f"within {CONVERGENCE_WAIT_SECONDS}s of the prefix being "
                f"published (poll interval: {SHORT_POLL_INTERVAL_SECONDS}s)."
            )
        finally:
            poller.stop()
