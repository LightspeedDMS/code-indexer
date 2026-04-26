"""
Unit tests for DependencyLatencyTracker - fire-and-forget latency recorder.

Story #680: External Dependency Latency Observability

Tests written FIRST following TDD methodology.
"""

import queue
import threading
import time
from pathlib import Path
from typing import Generator, List

import pytest

# ── Named constants: sample field names ───────────────────────────────────────
FIELD_DEP_NAME = "dependency_name"
FIELD_LATENCY_MS = "latency_ms"
FIELD_STATUS_CODE = "status_code"
FIELD_NODE_ID = "node_id"
FIELD_TIMESTAMP = "timestamp"

# ── Named constants: dependency names ─────────────────────────────────────────
DEFAULT_DEP_NAME = "voyageai_embed"
EMPTY_DEP_NAME = ""
DEP_NAME_PREFIX = "dep_"

# ── Named constants: latency / status values ───────────────────────────────────
DEFAULT_LATENCY_MS = 100.0
DEFAULT_STATUS_CODE = 200
ERROR_STATUS_CODE = -1
INVALID_NEGATIVE_LATENCY_MS = -1.0
OVERFLOW_EXTRA_LATENCY_MS = 1.0
STALE_SAMPLE_LATENCY_MS = 50.0
MIN_POSITIVE_LATENCY_MS = 0.0
FLOAT_TOLERANCE = 1.0  # ms tolerance for latency checks

# ── Named constants: buffer / index ───────────────────────────────────────────
BUFFER_MAXLEN = 10000
FIRST_ROW_INDEX = 0
MIN_EXPECTED_ROW_COUNT = 1
MAX_FAILING_INSERT_CALLS = 2
INITIAL_CALL_COUNT = 0

# ── Named constants: timing / intervals ───────────────────────────────────────
WRITER_FLUSH_TIMEOUT_S = 15.0
SHUTDOWN_TIMEOUT_S = 10
LIFECYCLE_SHUTDOWN_TIMEOUT_S = 5
FAST_FLUSH_INTERVAL_S = 0.05
STANDARD_FLUSH_INTERVAL_S = 0.1
STANDARD_RETENTION_S = 300.0
SHORT_RETENTION_S = 1.0
BACKEND_FAILURE_WAIT_S = 0.5
STALE_DELETE_WAIT_TIMEOUT_S = 5.0
STALE_POLL_INTERVAL_S = 0.1
TRACK_LATENCY_SLEEP_S = 0.001
MAX_RECORD_SAMPLE_DURATION_S = 0.010  # 10 ms upper bound for O(1) deque append

# ── Named constants: stale-sample values ──────────────────────────────────────
STALE_SAMPLE_AGE_S = 10000.0
TEST_NODE_ID = "node-1"

# ── Named constants: thread safety ────────────────────────────────────────────
THREAD_COUNT = 10
SAMPLES_PER_THREAD = 100
THREAD_JOIN_TIMEOUT_S = 5.0

# ── Named constants: query window ─────────────────────────────────────────────
WINDOW_START_TS = 0.0
WINDOW_END_OFFSET_S = 1.0

# ── Named constants: file names ───────────────────────────────────────────────
DB_FILENAME = "test_tracker.db"
STALE_TEST_DB_FILENAME = "stale_test.db"
LIFECYCLE_DB_FILENAME = "lifecycle.db"
DOUBLE_SHUTDOWN_DB_FILENAME = "double_shutdown.db"

# ── Named constants: exception / assertion messages ────────────────────────────
SIMULATED_BACKEND_FAILURE_MSG = "simulated backend failure"
INTENTIONAL_ERROR_MSG = "intentional test error"
TEST_RUNTIME_ERROR_MSG = "test error"
STALE_SAMPLES_ASSERT_MSG = "Stale samples should have been deleted"
THREAD_TIMEOUT_ASSERT_MSG = "Worker thread did not finish within timeout"
THREAD_SAFETY_ASSERT_PREFIX = "Thread safety violation: "

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_backend(tmp_path: Path, filename: str):
    """Create an initialized SQLite backend in tmp_path."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.dependency_latency_backend import (
        DependencyLatencyBackend,
    )

    db_path = str(tmp_path / filename)
    DatabaseSchema(db_path).initialize_database()
    return DependencyLatencyBackend(db_path)


def _make_tracker(
    backend,
    flush_interval_s: float = STANDARD_FLUSH_INTERVAL_S,
    retention_s: float = STANDARD_RETENTION_S,
):
    """Create a DependencyLatencyTracker (not started)."""
    from code_indexer.server.services.dependency_latency_tracker import (
        DependencyLatencyTracker,
    )

    return DependencyLatencyTracker(
        backend=backend,
        flush_interval_s=flush_interval_s,
        retention_s=retention_s,
    )


def _wait_for_rows(backend, timeout_s: float = WRITER_FLUSH_TIMEOUT_S) -> List:
    """Poll the backend until at least one sample is persisted or timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(FAST_FLUSH_INTERVAL_S)
        rows = backend.select_samples_for_window(
            WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
        )
        if rows:
            return rows
    return backend.select_samples_for_window(
        WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
    )


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def tracker(tmp_path: Path) -> Generator:
    """Create a started DependencyLatencyTracker with real SQLite backend."""
    backend = _make_backend(tmp_path, DB_FILENAME)
    t = _make_tracker(backend)
    t.start()
    yield t
    t.shutdown(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.fixture
def stopped_tracker(tmp_path: Path) -> Generator:
    """Create a DependencyLatencyTracker that is NOT started."""
    backend = _make_backend(tmp_path, DB_FILENAME)
    t = _make_tracker(backend)
    yield t


# ── Test classes ───────────────────────────────────────────────────────────────


@pytest.mark.slow
class TestDependencyLatencyTrackerRecordSample:
    """Tests for record_sample method - fire-and-forget contract."""

    def test_record_sample_returns_immediately(self, tracker) -> None:
        """record_sample() returns without blocking."""
        start = time.monotonic()
        tracker.record_sample(DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE)
        elapsed = time.monotonic() - start
        assert elapsed < MAX_RECORD_SAMPLE_DURATION_S

    def test_record_sample_never_raises_on_bad_dep_name(self, tracker) -> None:
        """record_sample() swallows all exceptions, including bad dep names."""
        tracker.record_sample(EMPTY_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE)
        # Deliberately testing runtime invalid input (None dep_name) that typed
        # signatures cannot express; verifies no exception is raised.
        tracker.record_sample(None, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE)  # type: ignore[arg-type]

    def test_record_sample_never_raises_on_bad_latency(self, tracker) -> None:
        """record_sample() swallows all exceptions, including bad latency values."""
        tracker.record_sample(
            DEFAULT_DEP_NAME, INVALID_NEGATIVE_LATENCY_MS, DEFAULT_STATUS_CODE
        )
        # Deliberately testing runtime invalid input (None latency) that typed
        # signatures cannot express; verifies no exception is raised.
        tracker.record_sample(DEFAULT_DEP_NAME, None, DEFAULT_STATUS_CODE)  # type: ignore[arg-type]

    def test_record_sample_buffer_overflow_drops_oldest(self, tracker) -> None:
        """record_sample() drops oldest entry when buffer is full (maxlen=10000)."""
        for i in range(BUFFER_MAXLEN):
            tracker.record_sample(
                f"{DEP_NAME_PREFIX}{i % THREAD_COUNT}", float(i), DEFAULT_STATUS_CODE
            )
        tracker.record_sample(
            DEFAULT_DEP_NAME,
            DEFAULT_LATENCY_MS + OVERFLOW_EXTRA_LATENCY_MS,
            DEFAULT_STATUS_CODE,
        )
        assert len(tracker._buffer) == BUFFER_MAXLEN

    def test_record_sample_is_thread_safe(self, tracker) -> None:
        """record_sample() can be called concurrently from many threads."""
        error_q: queue.Queue = queue.Queue()

        def record_many():
            try:
                for _ in range(SAMPLES_PER_THREAD):
                    tracker.record_sample(
                        DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE
                    )
            except Exception as exc:
                error_q.put(exc)

        threads = [threading.Thread(target=record_many) for _ in range(THREAD_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=THREAD_JOIN_TIMEOUT_S)
            assert not t.is_alive(), THREAD_TIMEOUT_ASSERT_MSG

        errors = []
        while not error_q.empty():
            errors.append(error_q.get_nowait())
        assert errors == [], f"{THREAD_SAFETY_ASSERT_PREFIX}{errors}"


@pytest.mark.slow
class TestDependencyLatencyTrackerWriterThread:
    """Tests for writer daemon thread - flushes and deletes stale samples."""

    def test_writer_flushes_to_backend(self, tracker) -> None:
        """Writer thread flushes buffered samples to SQLite backend."""
        tracker.record_sample(DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE)
        rows = _wait_for_rows(tracker._backend)
        assert len(rows) >= MIN_EXPECTED_ROW_COUNT
        assert rows[FIRST_ROW_INDEX][FIELD_DEP_NAME] == DEFAULT_DEP_NAME
        assert (
            abs(rows[FIRST_ROW_INDEX][FIELD_LATENCY_MS] - DEFAULT_LATENCY_MS)
            < FLOAT_TOLERANCE
        )

    def test_writer_survives_backend_exception(self, tmp_path: Path) -> None:
        """Writer thread continues after backend raises an exception."""

        class FailingBackend:
            call_count = INITIAL_CALL_COUNT

            def insert_batch(self, samples):
                self.call_count += MIN_EXPECTED_ROW_COUNT
                if self.call_count <= MAX_FAILING_INSERT_CALLS:
                    raise RuntimeError(SIMULATED_BACKEND_FAILURE_MSG)

            def delete_older_than(self, cutoff):
                pass

            def select_samples_for_window(self, start, end):
                return []

        t = _make_tracker(FailingBackend(), flush_interval_s=FAST_FLUSH_INTERVAL_S)
        t.start()
        try:
            t.record_sample(DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE)
            time.sleep(BACKEND_FAILURE_WAIT_S)
            assert t._writer_thread is not None
            assert t._writer_thread.is_alive()
        finally:
            t.shutdown(timeout=SHUTDOWN_TIMEOUT_S)

    def test_writer_deletes_stale_samples(self, tmp_path: Path) -> None:
        """Writer thread deletes samples older than retention_s."""
        backend = _make_backend(tmp_path, STALE_TEST_DB_FILENAME)
        old_sample = {
            FIELD_NODE_ID: TEST_NODE_ID,
            FIELD_DEP_NAME: DEFAULT_DEP_NAME,
            FIELD_TIMESTAMP: time.time() - STALE_SAMPLE_AGE_S,
            FIELD_LATENCY_MS: STALE_SAMPLE_LATENCY_MS,
            FIELD_STATUS_CODE: DEFAULT_STATUS_CODE,
        }
        backend.insert_batch([old_sample])

        t = _make_tracker(
            backend,
            flush_interval_s=FAST_FLUSH_INTERVAL_S,
            retention_s=SHORT_RETENTION_S,
        )
        t.start()
        try:
            deadline = time.monotonic() + STALE_DELETE_WAIT_TIMEOUT_S
            while time.monotonic() < deadline:
                time.sleep(STALE_POLL_INTERVAL_S)
                rows = backend.select_samples_for_window(
                    WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
                )
                if not rows:
                    break
            rows = backend.select_samples_for_window(
                WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
            )
            assert rows == [], STALE_SAMPLES_ASSERT_MSG
        finally:
            t.shutdown(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.mark.slow
class TestDependencyLatencyTrackerLifecycle:
    """Tests for start() / shutdown() lifecycle."""

    def test_start_launches_writer_thread(self, tracker) -> None:
        """start() launches a daemon writer thread."""
        assert tracker._writer_thread is not None
        assert tracker._writer_thread.is_alive()
        assert tracker._writer_thread.daemon is True

    def test_shutdown_stops_writer_thread(self, tmp_path: Path) -> None:
        """shutdown() stops the writer thread within timeout."""
        backend = _make_backend(tmp_path, LIFECYCLE_DB_FILENAME)
        t = _make_tracker(backend)
        t.start()
        assert t._writer_thread is not None
        assert t._writer_thread.is_alive()
        t.shutdown(timeout=LIFECYCLE_SHUTDOWN_TIMEOUT_S)
        assert not t._writer_thread.is_alive()

    def test_double_shutdown_is_safe(self, tmp_path: Path) -> None:
        """Calling shutdown() twice does not raise."""
        backend = _make_backend(tmp_path, DOUBLE_SHUTDOWN_DB_FILENAME)
        t = _make_tracker(backend)
        t.start()
        t.shutdown(timeout=LIFECYCLE_SHUTDOWN_TIMEOUT_S)
        t.shutdown(timeout=LIFECYCLE_SHUTDOWN_TIMEOUT_S)

    def test_record_before_start_is_safe(self, stopped_tracker) -> None:
        """record_sample() before start() does not raise."""
        stopped_tracker.record_sample(
            DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE
        )


@pytest.mark.slow
class TestDependencyLatencyTrackerContextManager:
    """Tests for track_latency() context manager."""

    def test_track_latency_records_sample_on_exit(self, tracker) -> None:
        """track_latency() context manager records a sample when block exits normally."""
        with tracker.track_latency(
            DEFAULT_DEP_NAME, expected_status_code=DEFAULT_STATUS_CODE
        ):
            time.sleep(TRACK_LATENCY_SLEEP_S)

        rows = _wait_for_rows(tracker._backend)
        assert len(rows) >= MIN_EXPECTED_ROW_COUNT
        assert rows[FIRST_ROW_INDEX][FIELD_DEP_NAME] == DEFAULT_DEP_NAME
        assert rows[FIRST_ROW_INDEX][FIELD_LATENCY_MS] > MIN_POSITIVE_LATENCY_MS

    def test_track_latency_records_error_status_on_exception(self, tracker) -> None:
        """track_latency() records status_code=-1 when an exception is raised."""
        with pytest.raises(ValueError):
            with tracker.track_latency(
                DEFAULT_DEP_NAME, expected_status_code=DEFAULT_STATUS_CODE
            ):
                raise ValueError(INTENTIONAL_ERROR_MSG)

        rows = _wait_for_rows(tracker._backend)
        assert len(rows) >= MIN_EXPECTED_ROW_COUNT
        assert rows[FIRST_ROW_INDEX][FIELD_STATUS_CODE] == ERROR_STATUS_CODE

    def test_track_latency_does_not_swallow_exception(self, tracker) -> None:
        """track_latency() re-raises exceptions from the block."""
        with pytest.raises(RuntimeError, match=TEST_RUNTIME_ERROR_MSG):
            with tracker.track_latency(
                DEFAULT_DEP_NAME, expected_status_code=DEFAULT_STATUS_CODE
            ):
                raise RuntimeError(TEST_RUNTIME_ERROR_MSG)


# ── Tests: module-level singleton accessor ────────────────────────────────────


def _reset_singleton() -> None:
    """Clear the module-level tracker singleton to isolate tests."""
    from code_indexer.server.services.dependency_latency_tracker import set_instance

    set_instance(None)


class TestDependencyLatencyTrackerSingleton:
    """Tests for set_instance() / get_instance() module-level singleton."""

    def setup_method(self) -> None:
        _reset_singleton()

    def teardown_method(self) -> None:
        _reset_singleton()

    def test_get_instance_returns_none_before_set(self) -> None:
        """get_instance() returns None when no tracker has been registered."""
        from code_indexer.server.services.dependency_latency_tracker import (
            get_instance,
        )

        assert get_instance() is None

    def test_set_instance_then_get_instance_returns_same_object(self, tracker) -> None:
        """get_instance() returns exactly the object passed to set_instance()."""
        from code_indexer.server.services.dependency_latency_tracker import (
            get_instance,
            set_instance,
        )

        set_instance(tracker)
        assert get_instance() is tracker

    def test_set_instance_none_clears_registry(self, tracker) -> None:
        """set_instance(None) clears the singleton so get_instance() returns None."""
        from code_indexer.server.services.dependency_latency_tracker import (
            get_instance,
            set_instance,
        )

        set_instance(tracker)
        set_instance(None)
        assert get_instance() is None

    def test_set_instance_replaces_previous_tracker(self, tracker) -> None:
        """set_instance() with a new tracker replaces the previously registered one."""
        from code_indexer.server.services.dependency_latency_tracker import (
            get_instance,
            set_instance,
        )

        class _AnotherTracker:
            def record_sample(self, dep: str, ms: float, code: int) -> None:
                pass

        second = _AnotherTracker()
        set_instance(tracker)
        set_instance(second)
        assert get_instance() is second
