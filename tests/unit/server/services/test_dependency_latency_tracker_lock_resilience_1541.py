"""
Bug #1541: DependencyLatencyTracker's writer thread must survive transient
SQLite "database is locked" / "database is busy" contention instead of
permanently terminating after 5 consecutive occurrences.

These tests use a REAL SQLite database file, a REAL competing connection
holding a real BEGIN EXCLUSIVE transaction, and a REAL writer thread -- no
mocking of the store and no monkeypatching of production code.

Production's DatabaseConnectionManager.get_connection() (out of scope for
this fix -- see the bug's file-scope restriction) hardcodes
`PRAGMA busy_timeout = 30000` (30s). Reproducing >=5 genuine, consecutive
"database is locked" failures through that exact path would require holding
a competing lock for minutes (5 * up to 30s each) -- empirically verified
against real sqlite3 before writing these tests. To keep the tests fast
while still exercising REAL SQLite locking semantics and the REAL
DependencyLatencyTracker writer loop under test, the lock-contention tests
(a)/(b) below use a small, self-contained backend
(_FastBusyTimeoutBackend) that issues the EXACT SAME SQL against the EXACT
SAME schema as production's DependencyLatencyBackend, differing only in
that it opens its own connections with a short busy_timeout instead of
production's hardcoded 30000ms. This is not a mock: every read/write is a
real SQLite operation against a real file on disk, subject to real lock
contention.

Test (c) exercises the genuine closed-database terminal path directly
against the REAL production DependencyLatencyBackend / DatabaseConnectionManager
-- closing a connection raises immediately regardless of busy_timeout, so no
speed workaround is needed there.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Generator, List, Optional

import pytest

from code_indexer.server.storage.dependency_latency_backend import LatencySample

# ── Named constants ────────────────────────────────────────────────────────────
DB_FILENAME = "lock_resilience_1541.db"
FAST_BUSY_TIMEOUT_MS = 20
MIN_VALID_BUSY_TIMEOUT_MS = 0
MAX_VALID_BUSY_TIMEOUT_MS = 60_000
FLUSH_INTERVAL_S = 0.05
RETENTION_S = 300.0
SHUTDOWN_TIMEOUT_S = 10
LOCK_THREAD_JOIN_TIMEOUT_S = 5.0

DEFAULT_DEP_NAME = "voyageai_embed"
DEFAULT_LATENCY_MS = 42.0
DEFAULT_STATUS_CODE = 200

# Long enough that, at a real per-attempt cost of roughly
# FLUSH_INTERVAL_S + (FAST_BUSY_TIMEOUT_MS / 1000) per failure, the writer
# thread would exceed the pre-#1541 threshold of 5 consecutive lock failures
# and terminate well before this hold ends.
CONTENTION_HOLD_DURATION_S = 1.5

# Fixed wall-clock contention window for the anti-spin test.
SPIN_TEST_HOLD_DURATION_S = 2.0

# A hot, unbacked-off retry loop would attempt roughly
# SPIN_TEST_HOLD_DURATION_S / (FLUSH_INTERVAL_S + FAST_BUSY_TIMEOUT_MS/1000)
# ~= 2.0 / 0.07 ~= 28 times. A backed-off writer must do meaningfully fewer.
MAX_ACCEPTABLE_ATTEMPTS_DURING_CONTENTION = 20

POST_RELEASE_PERSIST_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.05

WINDOW_START_TS = 0.0
WINDOW_END_OFFSET_S = 10.0

TRACKER_LOGGER_NAME = "code_indexer.server.services.dependency_latency_tracker"

_module_logger = logging.getLogger(__name__)


def _validate_busy_timeout_ms(value: int) -> None:
    """Raise ValueError unless value is a bounded, non-bool int (SQL-safe)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"busy_timeout_ms must be an int, got {type(value).__name__}")
    if not (MIN_VALID_BUSY_TIMEOUT_MS <= value <= MAX_VALID_BUSY_TIMEOUT_MS):
        raise ValueError(
            f"busy_timeout_ms must be between {MIN_VALID_BUSY_TIMEOUT_MS} and "
            f"{MAX_VALID_BUSY_TIMEOUT_MS}, got {value}"
        )


# ── Real, non-mock SQLite backend with a short busy_timeout ────────────────────


class _FastBusyTimeoutBackend:
    """
    Real SQLite-backed backend for latency samples -- same table/SQL as
    production's DependencyLatencyBackend -- but opening its own connections
    with a short, validated busy_timeout instead of relying on
    DatabaseConnectionManager's hardcoded 30000ms.

    This is not a mock: every call performs a genuine SQLite transaction
    against a real file on disk, and is subject to real lock contention from
    any other real connection open on the same file.
    """

    def __init__(self, db_path: str, busy_timeout_ms: int) -> None:
        _validate_busy_timeout_ms(busy_timeout_ms)
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        except Exception:
            conn.close()
            raise
        return conn

    def insert_batch(self, samples: List[LatencySample]) -> None:
        if not samples:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                conn.executemany(
                    """INSERT INTO dependency_latency_samples
                       (node_id, dependency_name, timestamp, latency_ms, status_code)
                       VALUES (:node_id, :dependency_name, :timestamp,
                               :latency_ms, :status_code)""",
                    samples,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def delete_older_than(self, cutoff_timestamp: float) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                conn.execute(
                    "DELETE FROM dependency_latency_samples WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def select_samples_for_window(
        self, start_time: float, end_time: float
    ) -> List[LatencySample]:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """SELECT node_id, dependency_name, timestamp, latency_ms, status_code
                   FROM dependency_latency_samples
                   WHERE timestamp >= ? AND timestamp <= ?
                   ORDER BY timestamp ASC""",
                (start_time, end_time),
            )
            rows = cursor.fetchall()
            return [
                LatencySample(
                    node_id=row[0],
                    dependency_name=row[1],
                    timestamp=row[2],
                    latency_ms=row[3],
                    status_code=row[4],
                )
                for row in rows
            ]
        finally:
            conn.close()


# ── Real competing lock holder ──────────────────────────────────────────────────


class _CompetingExclusiveLock:
    """
    Holds a REAL, separate SQLite connection's BEGIN EXCLUSIVE transaction
    open for a controlled duration, on a background thread, to produce
    genuine write-lock contention against the tracker's own connection(s).

    Any error acquiring the lock is captured and re-raised from acquire();
    release() verifies the holder thread actually terminated and re-raises
    any captured error as a safety net. Cleanup (rollback/close) failures in
    the holder thread are non-critical (the connection is being discarded
    regardless) but are logged, never silently discarded.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._release_event = threading.Event()
        self._acquired_event = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("BEGIN EXCLUSIVE")
            self._acquired_event.set()
            self._release_event.wait()
        except BaseException as exc:  # noqa: BLE001 - propagate to test thread
            self._error = exc
            self._acquired_event.set()
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception as exc:
                    _module_logger.debug(
                        "Competing lock holder: rollback failed during "
                        "cleanup (non-critical -- connection is being "
                        "closed regardless): %s",
                        exc,
                    )
                try:
                    conn.close()
                except Exception as exc:
                    _module_logger.debug(
                        "Competing lock holder: close failed during "
                        "cleanup (non-critical -- best-effort resource "
                        "release): %s",
                        exc,
                    )

    def acquire(self, timeout_s: float = 5.0) -> None:
        self._thread.start()
        acquired = self._acquired_event.wait(timeout=timeout_s)
        if not acquired:
            raise AssertionError("Failed to acquire competing EXCLUSIVE lock in time")
        if self._error is not None:
            raise self._error

    def release(self) -> None:
        self._release_event.set()
        self._thread.join(timeout=LOCK_THREAD_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            raise AssertionError(
                "Competing lock holder thread did not terminate within "
                f"{LOCK_THREAD_JOIN_TIMEOUT_S}s"
            )
        if self._error is not None:
            raise self._error


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path, filename: str = DB_FILENAME) -> str:
    """Create a real, initialized SQLite database file with the production schema."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / filename)
    DatabaseSchema(db_path).initialize_database()
    return db_path


def _make_production_backend(tmp_path: Path, filename: str = DB_FILENAME):
    """Create a real, initialized production DependencyLatencyBackend."""
    from code_indexer.server.storage.dependency_latency_backend import (
        DependencyLatencyBackend,
    )

    db_path = _make_db(tmp_path, filename)
    return DependencyLatencyBackend(db_path), db_path


def _make_tracker(backend, flush_interval_s: float = FLUSH_INTERVAL_S):
    from code_indexer.server.services.dependency_latency_tracker import (
        DependencyLatencyTracker,
    )

    return DependencyLatencyTracker(
        backend=backend,
        flush_interval_s=flush_interval_s,
        retention_s=RETENTION_S,
    )


def _wait_for_rows(backend, timeout_s: float = POST_RELEASE_PERSIST_TIMEOUT_S) -> List:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = backend.select_samples_for_window(
            WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
        )
        if rows:
            return list(rows)
        time.sleep(POLL_INTERVAL_S)
    return list(
        backend.select_samples_for_window(
            WINDOW_START_TS, time.time() + WINDOW_END_OFFSET_S
        )
    )


def _wait_until(
    predicate: Callable[[], bool], timeout_s: float, poll_s: float = POLL_INTERVAL_S
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


@pytest.fixture
def caplog_tracker(caplog) -> Generator:
    """Capture tracker logger output at DEBUG+ across all handlers."""
    caplog.set_level(logging.DEBUG, logger=TRACKER_LOGGER_NAME)
    yield caplog


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.slow
class TestWriterSurvivesTransientLockContention:
    """(a) The writer thread must survive genuine, sustained lock contention
    and still persist samples once the contention clears."""

    def test_writer_survives_and_persists_after_transient_lock_contention(
        self, tmp_path: Path
    ) -> None:
        db_path = _make_db(tmp_path)
        backend = _FastBusyTimeoutBackend(db_path, FAST_BUSY_TIMEOUT_MS)
        tracker = _make_tracker(backend)
        tracker.start()
        lock = _CompetingExclusiveLock(db_path)
        try:
            lock.acquire()
            tracker.record_sample(
                DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE
            )
            # Hold the lock long enough that, at the pre-#1541 termination
            # threshold, the writer thread would already be dead before we
            # release it below.
            time.sleep(CONTENTION_HOLD_DURATION_S)

            # The writer thread must still be alive DURING sustained
            # contention -- this is the assertion that fails against the
            # pre-#1541 behaviour (thread dies after 5 consecutive lock
            # failures, well before CONTENTION_HOLD_DURATION_S elapses).
            assert tracker._writer_thread is not None
            assert tracker._writer_thread.is_alive(), (
                "Writer thread must survive transient lock contention, "
                "not terminate permanently"
            )
        finally:
            lock.release()
            try:
                # Once contention clears, the sample must eventually be
                # persisted -- verified before shutdown so the writer's own
                # background flush (not just the final-flush-on-shutdown
                # path) is what's being proven.
                rows = _wait_for_rows(backend)
                assert len(rows) >= 1, (
                    "Sample must be persisted once lock contention clears -- "
                    "the writer thread must not have died during contention"
                )
                assert tracker._writer_thread.is_alive()
            finally:
                tracker.shutdown(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.mark.slow
class TestWriterBackoffBoundsRetries:
    """(b) Under persistent lock contention, the writer thread must not spin
    unboundedly -- retries must be bounded/backed-off, not a hot loop, while
    the thread stays alive (does not give up)."""

    def test_writer_backoff_bounds_retry_attempts_under_persistent_contention(
        self, tmp_path: Path
    ) -> None:
        db_path = _make_db(tmp_path)
        backend = _FastBusyTimeoutBackend(db_path, FAST_BUSY_TIMEOUT_MS)
        tracker = _make_tracker(backend)
        tracker.start()
        lock = _CompetingExclusiveLock(db_path)
        try:
            lock.acquire()
            # Also exercise the INSERT path (not just the always-running
            # DELETE/prune path) under lock contention.
            tracker.record_sample(
                DEFAULT_DEP_NAME, DEFAULT_LATENCY_MS, DEFAULT_STATUS_CODE
            )
            time.sleep(SPIN_TEST_HOLD_DURATION_S)

            # Thread must still be alive -- it must not have given up.
            assert tracker._writer_thread is not None
            assert tracker._writer_thread.is_alive(), (
                "Writer thread must remain alive under persistent "
                "contention, not terminate"
            )

            attempts = tracker.get_health_status()["total_flush_attempts"]
        finally:
            lock.release()
            tracker.shutdown(timeout=SHUTDOWN_TIMEOUT_S)

        assert 0 < attempts <= MAX_ACCEPTABLE_ATTEMPTS_DURING_CONTENTION, (
            f"Expected a bounded, backed-off attempt count (>0 and <= "
            f"{MAX_ACCEPTABLE_ATTEMPTS_DURING_CONTENTION}), got {attempts} -- "
            "the writer loop appears to be spinning rather than backing off"
        )


@pytest.mark.slow
class TestTerminalStateIsObservable:
    """(c) If a terminal condition is genuinely reached (closed database),
    it must be observable -- logged loudly and queryable -- not silent."""

    def test_closed_database_termination_is_observable_not_silent(
        self, tmp_path: Path, caplog_tracker
    ) -> None:
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        backend, db_path = _make_production_backend(tmp_path)
        tracker = _make_tracker(backend)
        tracker.start()
        try:
            # Let the writer thread run at least one real cycle so its own
            # thread-local SQLite connection is created and registered.
            time.sleep(FLUSH_INTERVAL_S * 4)

            # Genuinely close every registered real connection (including
            # the writer thread's own thread-local connection object) -- a
            # real sqlite3.Connection.close(), not a mock.
            conn_manager = DatabaseConnectionManager.get_instance(db_path)
            conn_manager.close_all()

            stopped = _wait_until(
                lambda: tracker._writer_thread is not None
                and not tracker._writer_thread.is_alive(),
                timeout_s=5.0,
            )
            assert stopped, "Writer thread must stop after the database is closed"

            # Observable via a queryable health-status API -- not silent.
            # This is the primary "not silent" channel for THIS specific
            # path: Bug #1227 (a separate, pinned regression) deliberately
            # keeps the log line at DEBUG (not WARNING/ERROR) because it
            # fires on every routine graceful shutdown teardown, not only
            # on a genuine anomaly -- so log severity cannot be the signal
            # here without reintroducing that noise. A queryable API is.
            health = tracker.get_health_status()
            assert health["alive"] is False
            assert health["terminated"] is True
            assert health["termination_reason"] is not None
            assert "closed" in health["termination_reason"].lower()

            # Still logged (at DEBUG or louder, per Bug #1227) -- not
            # entirely absent from diagnostics, just not alarm-level.
            closed_db_log_records = [
                r
                for r in caplog_tracker.records
                if r.name == TRACKER_LOGGER_NAME
                and r.levelno >= logging.DEBUG
                and "closed" in r.getMessage().lower()
            ]
            assert closed_db_log_records, (
                "Closed-database termination must still be logged (at "
                "DEBUG, per Bug #1227) -- never entirely absent from logs"
            )
        finally:
            tracker.shutdown(timeout=SHUTDOWN_TIMEOUT_S)
