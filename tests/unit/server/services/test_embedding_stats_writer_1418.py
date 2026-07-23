"""Unit tests for the EmbeddingStatsWriter registry, NoOpWriter, and
InProcessAsyncWriter (Story #1418 Phase 1 of 3 -- foundation only, see
issue #1418 for full scope).
"""

import threading
import time
from typing import List


class _StubBackend:
    """Collects inserted batches so tests can assert on them."""

    def __init__(self, fail_count: int = 0):
        self.batches: List[list] = []
        self._fail_count = fail_count  # raise on first N insert_batch calls
        self._call_count = 0
        self._lock = threading.Lock()

    def insert_batch(self, records: list) -> None:
        with self._lock:
            self._call_count += 1
            if self._call_count <= self._fail_count:
                raise RuntimeError("Simulated backend failure")
            self.batches.append(list(records))

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    def total_records(self) -> int:
        return sum(len(b) for b in self.batches)


def _make_call(**overrides):
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    defaults = dict(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=1,
        token_count=10,
        batch_size=1,
        purpose="query",
        success=True,
        latency_ms=5,
        occurred_at=time.time(),
    )
    defaults.update(overrides)
    return EmbeddingCallRecord(**defaults)


# ---------------------------------------------------------------------------
# EmbeddingStatsWriter registry
# ---------------------------------------------------------------------------


class TestEmbeddingStatsWriterRegistryDefault:
    def teardown_method(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        EmbeddingStatsWriter._active = None

    def test_get_active_defaults_to_noop_writer(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            NoOpWriter,
        )

        EmbeddingStatsWriter._active = None
        assert isinstance(EmbeddingStatsWriter.get_active(), NoOpWriter)

    def test_get_active_returns_same_default_instance_repeatedly(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        EmbeddingStatsWriter._active = None
        assert EmbeddingStatsWriter.get_active() is EmbeddingStatsWriter.get_active()


class TestEmbeddingStatsWriterRegistrySetActive:
    def teardown_method(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        EmbeddingStatsWriter._active = None

    def test_set_active_installs_custom_writer(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            NoOpWriter,
        )

        custom = NoOpWriter()
        EmbeddingStatsWriter.set_active(custom)
        assert EmbeddingStatsWriter.get_active() is custom

    def test_reset_active_to_none_falls_back_to_noop(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            NoOpWriter,
        )

        EmbeddingStatsWriter.set_active(NoOpWriter())
        EmbeddingStatsWriter._active = None
        assert isinstance(EmbeddingStatsWriter.get_active(), NoOpWriter)


# ---------------------------------------------------------------------------
# NoOpWriter
# ---------------------------------------------------------------------------


class TestNoOpWriter:
    def test_record_does_not_raise(self):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter

        NoOpWriter().record(_make_call())

    def test_record_repeated_calls_have_no_side_effects(self):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter

        writer = NoOpWriter()
        for _ in range(50):
            writer.record(_make_call())

    def test_flush_does_not_raise(self):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter

        NoOpWriter().flush()


# ---------------------------------------------------------------------------
# InProcessAsyncWriter
# ---------------------------------------------------------------------------


class TestInProcessAsyncWriterRecord:
    def test_record_enqueues_without_blocking(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend())
        start = time.monotonic()
        writer.record(_make_call())
        elapsed = time.monotonic() - start
        assert elapsed < 0.05
        assert writer._queue.qsize() == 1

    def test_record_never_raises_on_full_queue(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend(), maxsize=2)
        writer.record(_make_call())
        writer.record(_make_call())
        writer.record(_make_call())  # queue full -- must not raise


class TestInProcessAsyncWriterFlush:
    def test_flush_drains_buffer_into_one_insert_batch_call(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend()
        writer = InProcessAsyncWriter(backend)
        for _ in range(5):
            writer.record(_make_call())
        writer.flush()
        assert backend.call_count == 1
        assert backend.total_records() == 5

    def test_flush_empty_queue_does_not_call_insert_batch(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend()
        InProcessAsyncWriter(backend).flush()
        assert backend.call_count == 0

    def test_flush_exception_is_caught_and_not_raised(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend(fail_count=1)
        writer = InProcessAsyncWriter(backend)
        writer.record(_make_call())
        writer.flush()  # must not raise despite backend failure


class TestInProcessAsyncWriterFlushBatchCap:
    def test_flush_drains_across_multiple_insert_calls_when_over_cap(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
            _MAX_DRAIN_BATCH,
        )

        backend = _StubBackend()
        writer = InProcessAsyncWriter(backend, maxsize=_MAX_DRAIN_BATCH * 2 + 10)
        record_count = _MAX_DRAIN_BATCH + 5
        for _ in range(record_count):
            writer.record(_make_call())
        writer.flush()
        assert backend.call_count == 2  # one full batch + one partial
        assert backend.total_records() == record_count


class TestInProcessAsyncWriterBackgroundLoop:
    def test_periodic_flush_drains_queue(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend()
        writer = InProcessAsyncWriter(backend, flush_interval_seconds=0.05)
        writer.start()
        writer.record(_make_call())
        writer.record(_make_call())
        time.sleep(0.3)
        writer.stop(timeout=2.0)
        assert backend.total_records() == 2
        assert backend.call_count == 1

    def test_start_is_idempotent(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend(), flush_interval_seconds=5.0)
        writer.start()
        first_thread = writer._thread
        writer.start()
        assert writer._thread is first_thread
        writer.stop(timeout=2.0)

    def test_stop_drains_remaining_records(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        backend = _StubBackend()
        writer = InProcessAsyncWriter(backend, flush_interval_seconds=5.0)
        writer.start()
        writer.record(_make_call())
        writer.stop(timeout=2.0)
        assert backend.total_records() == 1


class TestInProcessAsyncWriterBackgroundLoopMore:
    def test_stop_is_idempotent(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend(), flush_interval_seconds=5.0)
        writer.start()
        writer.stop(timeout=2.0)
        writer.stop(timeout=2.0)  # must not raise

    def test_backend_exception_does_not_crash_background_loop(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(
            _StubBackend(fail_count=10), flush_interval_seconds=0.05
        )
        writer.start()
        time.sleep(0.2)
        assert writer._thread is not None
        assert writer._thread.is_alive()
        writer.stop(timeout=2.0)

    def test_thread_is_daemon(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend(), flush_interval_seconds=5.0)
        writer.start()
        assert writer._thread.daemon is True
        writer.stop(timeout=2.0)


class TestInProcessAsyncWriterLoopDrainsFullQueuePerCycle:
    def test_single_periodic_tick_drains_all_records_beyond_max_batch(self):
        """MEDIUM-1 (#1418 code review): a single ``_loop()`` periodic-flush
        tick must drain the ENTIRE queue, not just one ``_MAX_DRAIN_BATCH``
        (1000)-capped batch. Reproduces the under-drain bug: sustained
        throughput above ``_MAX_DRAIN_BATCH`` records per
        ``flush_interval_seconds`` leaves excess records queued after a
        ``_drain()``-only tick, eventually saturating the bounded ``Queue``
        and silently dropping new records -- undercounting the exact
        heavy-indexing period this story exists to measure for vendor cost
        reconciliation.

        Drives exactly ONE periodic tick of ``_loop()`` deterministically
        (no real threading/sleeping) via a fake ``_stop.wait`` side effect
        that snapshots queue/backend state immediately after that tick's
        action runs but BEFORE ``_loop()``'s unconditional shutdown
        ``flush()`` call -- which would otherwise mop up any leftover
        records and mask the per-tick under-drain.
        """
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
            _MAX_DRAIN_BATCH,
        )

        backend = _StubBackend()
        record_count = _MAX_DRAIN_BATCH + 500  # over the single-_drain() cap
        writer = InProcessAsyncWriter(
            backend, flush_interval_seconds=30.0, maxsize=record_count + 10
        )
        for _ in range(record_count):
            writer.record(_make_call())

        captured: dict = {}
        wait_calls = {"count": 0}

        def _fake_wait(timeout=None):
            call_num = wait_calls["count"]
            wait_calls["count"] += 1
            if call_num == 1:
                # Called right after the first while-loop tick's action has
                # executed, before any second tick or the final shutdown
                # flush -- this is the true per-tick drain result.
                captured["qsize_after_first_tick"] = writer._queue.qsize()
                captured["records_after_first_tick"] = backend.total_records()
            return call_num >= 1  # False -> run tick 1; True -> stop the loop

        writer._stop.wait = _fake_wait  # type: ignore[method-assign]
        writer._loop()

        assert captured["qsize_after_first_tick"] == 0
        assert captured["records_after_first_tick"] == record_count


class TestInProcessAsyncWriterDefaults:
    def test_default_flush_interval_is_30_seconds(self):
        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        writer = InProcessAsyncWriter(_StubBackend())
        assert writer._flush_interval_seconds == 30.0


class TestInProcessAsyncWriterStopTimeout:
    def test_stop_logs_warning_when_thread_does_not_stop_in_time(self, caplog):
        import logging

        from code_indexer.server.services.embedding_stats_writer import (
            InProcessAsyncWriter,
        )

        class _SlowBackend:
            def insert_batch(self, records: list) -> None:
                time.sleep(0.3)

        # threading.Event.wait() wakes IMMEDIATELY once .set() is called (it
        # does not sleep for the full timeout) -- so the loop wakes right
        # away and runs its shutdown flush(). A slow backend.insert_batch()
        # keeps that shutdown flush running past the tiny stop(timeout=...),
        # deterministically (not flakily) triggering the "did not stop in
        # time" branch.
        writer = InProcessAsyncWriter(_SlowBackend(), flush_interval_seconds=30.0)
        writer.start()
        writer.record(_make_call())
        with caplog.at_level(logging.WARNING):
            writer.stop(timeout=0.01)

        assert any("did not stop within" in record.message for record in caplog.records)
        # Real cleanup: signal already set by stop(); wait for the actual exit.
        writer._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# CrossProcessBootstrapWriter -- Story #1418 Phase 2 of 3.
#
# Lives inside a `cidx index` child subprocess (bootstrapped via
# CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR -- see embedding_stats_child_wiring.py).
# It reuses InProcessAsyncWriter's buffer/background-flush machinery
# unchanged (same Queue, same daemon thread, same periodic-drain loop) --
# it differs only in HOW its backend connection is resolved (a
# concern external to this class), never in the flush mechanism itself.
# ---------------------------------------------------------------------------


class TestCrossProcessBootstrapWriterIsInProcessAsyncWriter:
    def test_is_subclass_of_in_process_async_writer(self):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
            InProcessAsyncWriter,
        )

        assert issubclass(CrossProcessBootstrapWriter, InProcessAsyncWriter)

    def test_construction_matches_in_process_async_writer_shape(self):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        backend = _StubBackend()
        writer = CrossProcessBootstrapWriter(backend, flush_interval_seconds=0.05)
        assert writer._backend is backend
        assert writer._flush_interval_seconds == 0.05


class TestCrossProcessBootstrapWriterRecordAndFlush:
    def test_record_enqueues_without_blocking(self):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        writer = CrossProcessBootstrapWriter(_StubBackend())
        start = time.monotonic()
        writer.record(_make_call())
        elapsed = time.monotonic() - start
        assert elapsed < 0.05
        assert writer._queue.qsize() == 1

    def test_flush_drains_buffer_into_one_insert_batch_call(self):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        backend = _StubBackend()
        writer = CrossProcessBootstrapWriter(backend)
        for _ in range(5):
            writer.record(_make_call())
        writer.flush()
        assert backend.call_count == 1
        assert backend.total_records() == 5


class TestCrossProcessBootstrapWriterBackgroundLoopAndExit:
    def test_periodic_flush_drains_queue(self):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        backend = _StubBackend()
        writer = CrossProcessBootstrapWriter(backend, flush_interval_seconds=0.05)
        writer.start()
        writer.record(_make_call())
        writer.record(_make_call())
        time.sleep(0.3)
        writer.stop(timeout=2.0)
        assert backend.total_records() == 2
        assert backend.call_count == 1

    def test_stop_on_process_exit_flushes_remaining_records(self):
        """Simulates the child subprocess's normal-completion exit path:
        stop() is called in a finally block and must flush any buffered
        records that had not yet hit the periodic flush interval."""
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        backend = _StubBackend()
        writer = CrossProcessBootstrapWriter(backend, flush_interval_seconds=30.0)
        writer.start()
        writer.record(_make_call())
        writer.record(_make_call())
        writer.record(_make_call())
        writer.stop(timeout=2.0)  # best-effort final flush on process exit
        assert backend.total_records() == 3

    def test_sigkill_loses_only_unflushed_tail_is_accepted_tradeoff(self):
        """Documents the accepted fail-open tradeoff: records enqueued but
        never flushed (e.g. because the process was SIGKILLed / OOM-killed
        before stop() ran) are simply lost -- consistent with this
        project's fail-open convention for observability data. This test
        proves the buffer holds unflushed records until a flush occurs
        (i.e. nothing is flushed prematurely / synchronously on record())."""
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
        )

        backend = _StubBackend()
        writer = CrossProcessBootstrapWriter(backend, flush_interval_seconds=30.0)
        writer.record(_make_call())  # never started, never flushed/stopped
        assert backend.call_count == 0
        assert writer._queue.qsize() == 1
