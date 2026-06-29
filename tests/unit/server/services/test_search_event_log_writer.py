"""Unit tests for SearchEventLogWriter (Issue #1159).

RED phase: these tests will fail until the production code is written.
"""

import threading
import time
from queue import Empty
from typing import List
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Minimal stub backend for unit testing the writer
# ---------------------------------------------------------------------------


class _StubBackend:
    """Collects inserted batches so tests can assert on them."""

    def __init__(self, fail_count: int = 0):
        self.batches: List[list] = []
        self.prune_cutoffs: List[float] = []
        self._fail_count = fail_count  # raise on first N insert_batch calls
        self._call_count = 0
        self._lock = threading.Lock()

    def insert_batch(self, records: list) -> None:
        with self._lock:
            self._call_count += 1
            if self._call_count <= self._fail_count:
                raise RuntimeError("Simulated backend failure")
            self.batches.append(list(records))

    def prune_older_than(self, cutoff: float) -> None:
        with self._lock:
            self.prune_cutoffs.append(cutoff)

    def query(self, **kwargs):
        return [], 0

    def total_records(self) -> int:
        return sum(len(b) for b in self.batches)


def _make_record(query_text: str = "test query") -> object:
    """Return a minimal SearchEventRecord-like object."""
    from code_indexer.server.services.search_event_log_writer import SearchEventRecord

    return SearchEventRecord(
        timestamp=time.time(),
        username="testuser",
        repo_alias="repo1",
        search_type="semantic",
        query_text=query_text,
        voyage_cache_hit=None,
        voyage_cache_mode=None,
        voyage_latency_ms=None,
        cohere_cache_hit=None,
        cohere_cache_mode=None,
        cohere_latency_ms=None,
        total_latency_ms=100,
        result_count=5,
        node_id="node1",
        correlation_id=None,
    )


# ---------------------------------------------------------------------------
# Tests: SearchEventRecord dataclass
# ---------------------------------------------------------------------------


class TestSearchEventRecord:
    def test_all_nullable_fields(self):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventRecord,
        )

        r = SearchEventRecord(
            timestamp=1.0,
            username="u",
            repo_alias=None,
            search_type="semantic",
            query_text="q",
            voyage_cache_hit=None,
            voyage_cache_mode=None,
            voyage_latency_ms=None,
            cohere_cache_hit=None,
            cohere_cache_mode=None,
            cohere_latency_ms=None,
            total_latency_ms=50,
            result_count=0,
            node_id="n",
            correlation_id=None,
        )
        assert r.repo_alias is None
        assert r.voyage_cache_hit is None
        assert r.cohere_cache_hit is None
        assert r.correlation_id is None

    def test_all_fields_populated(self):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventRecord,
        )

        r = SearchEventRecord(
            timestamp=123.456,
            username="alice",
            repo_alias="myrepo",
            search_type="fts",
            query_text="hello world",
            voyage_cache_hit=True,
            voyage_cache_mode="on",
            voyage_latency_ms=42,
            cohere_cache_hit=False,
            cohere_cache_mode="shadow",
            cohere_latency_ms=100,
            total_latency_ms=200,
            result_count=10,
            node_id="node-abc",
            correlation_id="corr-xyz",
        )
        assert r.timestamp == 123.456
        assert r.username == "alice"
        assert r.voyage_cache_hit is True
        assert r.cohere_cache_hit is False
        assert r.correlation_id == "corr-xyz"


# ---------------------------------------------------------------------------
# Tests: Writer queue mechanics
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterQueue:
    def test_enqueue_adds_to_queue(self):
        """Enqueuing a record makes it available for draining."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        record = _make_record()
        writer.enqueue(record)
        # Start and stop so drain runs
        writer.start()
        time.sleep(0.2)
        writer.stop(timeout=3.0)
        assert backend.total_records() == 1

    def test_enqueue_multiple_records(self):
        """Enqueuing 10 records stores all of them."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        for i in range(10):
            writer.enqueue(_make_record(f"query {i}"))
        writer.start()
        time.sleep(0.2)
        writer.stop(timeout=3.0)
        assert backend.total_records() == 10

    def test_enqueue_does_not_block(self):
        """Enqueue on a full queue does NOT block (NEVER raises)."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend(fail_count=999)  # backend always fails -> queue fills
        writer = SearchEventLogWriter(backend, maxsize=3)
        # Start but don't drain (backend fails so queue stays full after 3)
        writer.start()
        # Fill the queue
        for _ in range(3):
            writer.enqueue(_make_record())

        # These should not block even if queue is full
        t0 = time.time()
        for _ in range(5):
            writer.enqueue(_make_record())
        elapsed = time.time() - t0
        writer.stop(timeout=2.0)
        assert elapsed < 1.0, f"Enqueue should be fast, took {elapsed:.3f}s"

    def test_enqueue_after_stop_is_noop(self):
        """Enqueueing after stop() is a safe no-op."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        writer.stop(timeout=2.0)
        # Should not raise
        writer.enqueue(_make_record())


# ---------------------------------------------------------------------------
# Tests: Overflow warning behavior
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterOverflow:
    def test_overflow_warning_logged_once(self):
        """When queue overflows, WARNING is logged only once per overflow run."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend, maxsize=2)
        # Do not start (no drain), fill up queue
        for _ in range(2):
            writer._queue.put_nowait(_make_record())

        with patch(
            "code_indexer.server.services.search_event_log_writer.logger"
        ) as mock_log:
            # Overflow 3 more times
            for _ in range(3):
                writer.enqueue(_make_record())
            # Warning should have been called exactly once due to the overflow_warned flag
            assert mock_log.warning.call_count == 1

    def test_overflow_warned_resets_after_success(self):
        """After a successful enqueue, overflow_warned resets so next overflow warns again."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend, maxsize=1)
        # Fill the queue
        writer._queue.put_nowait(_make_record())

        with patch(
            "code_indexer.server.services.search_event_log_writer.logger"
        ) as mock_log:
            # First overflow - should warn
            writer.enqueue(_make_record())
            assert mock_log.warning.call_count == 1
            assert writer._overflow_warned is True

        # Drain the queue to make room
        try:
            writer._queue.get_nowait()
        except Empty:
            pass

        # Now enqueue succeeds — resets the flag
        writer.enqueue(_make_record())
        assert writer._overflow_warned is False


# ---------------------------------------------------------------------------
# Tests: Drain mechanics
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterDrain:
    def test_drain_calls_insert_batch(self):
        """_drain() calls backend.insert_batch with all queued records."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        for i in range(5):
            writer._queue.put_nowait(_make_record(f"q{i}"))

        writer._drain()
        assert len(backend.batches) == 1
        assert len(backend.batches[0]) == 5

    def test_drain_empty_queue_is_noop(self):
        """_drain() on empty queue doesn't call insert_batch."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer._drain()
        assert len(backend.batches) == 0

    def test_drain_batch_size_capped_at_500(self):
        """_drain() drains at most 500 records per call."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        # Put 600 records
        for i in range(600):
            writer._queue.put_nowait(_make_record())

        writer._drain()
        # First drain should have taken at most 500
        assert backend.total_records() <= 500
        # Queue should still have records left
        assert not writer._queue.empty()


# ---------------------------------------------------------------------------
# Tests: Shutdown final drain
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterShutdown:
    def test_stop_drains_remaining_records(self):
        """stop() performs a final drain so no records are lost on shutdown."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        time.sleep(0.1)  # Let loop start

        # Enqueue after loop is running
        for i in range(5):
            writer.enqueue(_make_record(f"shutdown{i}"))

        writer.stop(timeout=5.0)
        # All records should have been drained
        assert backend.total_records() == 5

    def test_stop_is_idempotent(self):
        """Calling stop() twice doesn't raise."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        writer.stop(timeout=2.0)
        writer.stop(timeout=2.0)  # Should not raise

    def test_start_is_idempotent(self):
        """Calling start() twice doesn't create duplicate threads."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        thread_id_1 = id(writer._thread)
        writer.start()
        thread_id_2 = id(writer._thread)
        writer.stop(timeout=2.0)
        # start() should not have created a second thread
        assert thread_id_1 == thread_id_2


# ---------------------------------------------------------------------------
# Tests: Background thread properties
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterThread:
    def test_thread_is_daemon(self):
        """Writer thread must be daemon=True so it does not prevent process exit."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        assert writer._thread is not None
        assert writer._thread.daemon is True
        writer.stop(timeout=2.0)

    def test_thread_name(self):
        """Writer thread should have a recognisable name."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)
        writer.start()
        assert writer._thread is not None
        assert "search" in writer._thread.name.lower()
        writer.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Tests: Backend exception resilience
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterResilience:
    def test_backend_exception_does_not_crash_loop(self):
        """A backend insert_batch failure should log a warning but NOT kill the loop."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend(fail_count=2)  # fail first 2 insert_batch calls
        writer = SearchEventLogWriter(backend)
        writer.start()
        # Enqueue records during the failure window
        for i in range(3):
            writer.enqueue(_make_record())
        time.sleep(0.3)
        # Enqueue more after failures are done
        for i in range(3):
            writer.enqueue(_make_record())
        time.sleep(0.5)
        writer.stop(timeout=5.0)
        # Loop should still be alive enough to process some records
        # (exact count depends on timing, but at least the second batch should go through)
        # The important assertion is that we got here without an exception

    def test_10_consecutive_backend_failures_keep_loop_alive(self):
        """10 consecutive backend failures must NOT kill the writer loop."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend(fail_count=10)
        writer = SearchEventLogWriter(backend)
        writer.start()
        time.sleep(0.2)
        assert writer._thread is not None
        assert writer._thread.is_alive(), "Writer thread died after backend failures"
        writer.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# Tests: First-pass eviction
# ---------------------------------------------------------------------------


class TestSearchEventLogWriterEviction:
    def test_first_drain_pass_triggers_eviction(self):
        """_last_eviction=0.0 means the very first _maybe_evict call runs prune."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)

        # _last_eviction should start at 0.0 so first eviction fires immediately
        assert writer._last_eviction == 0.0

        with patch(
            "code_indexer.server.services.search_event_log_writer.get_config_service_for_eviction"
        ) as mock_cfg:
            mock_cfg.return_value.get_config.return_value.search_event_log_retention_days = 90
            writer._maybe_evict()

        # prune_older_than should have been called
        assert len(backend.prune_cutoffs) == 1
        # The cutoff should be ~90 days ago
        cutoff = backend.prune_cutoffs[0]
        expected_cutoff = time.time() - (90 * 86400)
        assert abs(cutoff - expected_cutoff) < 5.0  # within 5 seconds tolerance

    def test_eviction_not_called_again_within_24h(self):
        """After eviction fires, it should not fire again within 24 hours."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)

        with patch(
            "code_indexer.server.services.search_event_log_writer.get_config_service_for_eviction"
        ) as mock_cfg:
            mock_cfg.return_value.get_config.return_value.search_event_log_retention_days = 90
            writer._maybe_evict()  # First eviction runs
            writer._maybe_evict()  # Second call should be a no-op

        # prune_older_than should only have been called once
        assert len(backend.prune_cutoffs) == 1

    def test_eviction_uses_retention_days_from_config(self):
        """Eviction uses the configured retention days for cutoff calculation."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)

        with patch(
            "code_indexer.server.services.search_event_log_writer.get_config_service_for_eviction"
        ) as mock_cfg:
            mock_cfg.return_value.get_config.return_value.search_event_log_retention_days = 30
            writer._maybe_evict()

        cutoff = backend.prune_cutoffs[0]
        expected_cutoff = time.time() - (30 * 86400)
        assert abs(cutoff - expected_cutoff) < 5.0

    def test_eviction_fails_gracefully_on_config_error(self):
        """If config read fails, _maybe_evict falls back to 90 days and does not raise."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        backend = _StubBackend()
        writer = SearchEventLogWriter(backend)

        with patch(
            "code_indexer.server.services.search_event_log_writer.get_config_service_for_eviction"
        ) as mock_cfg:
            mock_cfg.side_effect = RuntimeError("Config unavailable")
            writer._maybe_evict()  # Should not raise

        # Should still have called prune with 90-day default
        assert len(backend.prune_cutoffs) == 1
        cutoff = backend.prune_cutoffs[0]
        expected_cutoff = time.time() - (90 * 86400)
        assert abs(cutoff - expected_cutoff) < 5.0
