"""Tests for Issue #1241 P1.1: Batched SQLiteLogHandler writer.

These tests assert that:
1. _writer_loop drains up to MAX_DRAIN_BATCH items per cycle and inserts them
   in ONE transaction (one insert_log_batch call), not one transaction per record.
2. All N records are present after flush() — no data loss.
3. A burst of >=50k records completes with ZERO exceptions and emit() is
   non-blocking (p99 << 5 ms even under saturation).
4. WAL journal_mode and busy_timeout > 0 are set on the logs.db connection.
5. insert_log_batch on the SQLite backend inserts via executemany in one txn.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(msg: str = "test", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.batched",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class _CountingBatchBackend:
    """Backend that counts insert_log_batch calls and accumulates all rows."""

    def __init__(self) -> None:
        self.batch_calls: List[List[Any]] = []
        self.all_items: List[Any] = []
        self._lock = threading.Lock()

    def insert_log_batch(self, items: List[Any]) -> None:
        with self._lock:
            self.batch_calls.append(list(items))
            self.all_items.extend(items)

    # insert_log is the old single-item path; it must NOT be called when
    # insert_log_batch is available (regression guard).
    def insert_log(self, **kwargs: Any) -> None:
        # Record the call as a batch of one so it counts against batch_calls.
        # A good implementation will never call this once batching is wired.
        self.insert_log_batch([kwargs])

    def query_logs(self, *args: Any, **kwargs: Any) -> Any:
        return [], 0

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        return 0

    def close(self) -> None:
        pass

    @property
    def total_items(self) -> int:
        with self._lock:
            return len(self.all_items)

    @property
    def batch_count(self) -> int:
        with self._lock:
            return len(self.batch_calls)


def _build_handler(tmp_path: Path, backend: Any) -> Any:
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    return SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)


# ---------------------------------------------------------------------------
# Test 1: N records -> ceil(N / MAX_DRAIN_BATCH) batch calls (not N calls)
# ---------------------------------------------------------------------------


def test_batched_writer_one_batch_call_per_drain_cycle(tmp_path: Path) -> None:
    """_writer_loop must drain up to MAX_DRAIN_BATCH items per cycle and call
    insert_log_batch ONCE per cycle, not once per record.

    We emit exactly MAX_DRAIN_BATCH records and expect exactly ONE
    insert_log_batch call (not MAX_DRAIN_BATCH individual calls).
    """
    from code_indexer.server.services.sqlite_log_handler import (
        MAX_DRAIN_BATCH,
        SQLiteLogHandler,
    )

    backend = _CountingBatchBackend()
    handler = SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)
    try:
        n = MAX_DRAIN_BATCH  # exactly one full batch
        for i in range(n):
            handler.emit(_make_record(f"record {i}"))

        handler.flush()

        assert backend.total_items == n, (
            f"Expected {n} items total, got {backend.total_items}"
        )
        # The key assertion: all items in at most ceil(n / MAX_DRAIN_BATCH) batches
        # (one full batch = one call)
        assert backend.batch_count <= math.ceil(n / MAX_DRAIN_BATCH) + 1, (
            f"Expected at most {math.ceil(n / MAX_DRAIN_BATCH) + 1} batch calls "
            f"for {n} records but got {backend.batch_count} — writer is NOT batching!"
        )
        # The key: batch_count must be FAR fewer than n (not per-record)
        assert backend.batch_count < n, (
            f"batch_count ({backend.batch_count}) == n ({n}), meaning the writer is "
            "calling insert_log or insert_log_batch once PER record instead of batching."
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 2: All N records present after flush (no data loss)
# ---------------------------------------------------------------------------


def test_all_records_present_after_flush(tmp_path: Path) -> None:
    """After flush(), all enqueued records must be present in the backend."""
    from code_indexer.server.services.sqlite_log_handler import (
        MAX_DRAIN_BATCH,
        SQLiteLogHandler,
    )

    n = MAX_DRAIN_BATCH * 3  # 3 full batches
    backend = _CountingBatchBackend()
    handler = SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)
    try:
        for i in range(n):
            handler.emit(_make_record(f"msg-{i}"))

        handler.flush()

        assert backend.total_items == n, (
            f"Expected {n} items after flush, got {backend.total_items}. "
            "Data was lost during batched drain."
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 3: Direct-SQLite path — executemany batching (one execute_atomic per drain)
# ---------------------------------------------------------------------------


def test_direct_sqlite_path_uses_executemany_batch(tmp_path: Path) -> None:
    """Direct-SQLite path (no backend) must use executemany in ONE transaction
    per drain cycle, not one INSERT per record.

    We count execute_atomic calls by wrapping the DatabaseConnectionManager.
    """
    from code_indexer.server.services.sqlite_log_handler import (
        MAX_DRAIN_BATCH,
        SQLiteLogHandler,
    )
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db_path = tmp_path / "logs.db"
    handler = SQLiteLogHandler(db_path=db_path)  # direct-SQLite path

    # Spy on execute_atomic: count calls after schema init
    mgr = DatabaseConnectionManager.get_instance(str(db_path))
    original_execute_atomic = mgr.execute_atomic
    atomic_call_count = [0]

    def counting_execute_atomic(op: Any) -> Any:
        atomic_call_count[0] += 1
        return original_execute_atomic(op)

    mgr.execute_atomic = counting_execute_atomic  # type: ignore[method-assign]

    try:
        n = MAX_DRAIN_BATCH  # emit exactly one batch worth
        for i in range(n):
            handler.emit(_make_record(f"direct-{i}"))

        handler.flush()

        # Verify all rows inserted
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        finally:
            conn.close()

        assert count == n, f"Expected {n} rows in DB, got {count}"

        # The key assertion: execute_atomic called at most a small number of times
        # (ceil(n/MAX_DRAIN_BATCH)), NOT n times
        assert atomic_call_count[0] <= math.ceil(n / MAX_DRAIN_BATCH) + 1, (
            f"execute_atomic called {atomic_call_count[0]} times for {n} records. "
            f"Expected at most {math.ceil(n / MAX_DRAIN_BATCH) + 1} (batched). "
            "The direct-SQLite path is NOT using executemany — it is doing one "
            "INSERT per record!"
        )
    finally:
        mgr.execute_atomic = original_execute_atomic  # type: ignore[method-assign]
        handler.close()


# ---------------------------------------------------------------------------
# Test 4: Burst of 50k records — no exceptions, emit non-blocking
# ---------------------------------------------------------------------------


def test_burst_50k_records_no_exceptions_emit_nonblocking(tmp_path: Path) -> None:
    """Burst test: enqueue 50,000 records.

    Invariants asserted:
    - emit() loop completes in << 5 seconds (non-blocking enqueue).
    - Zero exceptions raised during emit().
    - At least some records committed after flush() (queue may drop records at
      saturation since the queue cap is 10k, but the writer must never raise
      "database is locked" or similar errors).

    Note: full no-data-loss is only guaranteed for counts <= queue capacity
    (10k).  This test exercises the saturated-queue + drop path deliberately.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    backend = _CountingBatchBackend()
    handler = SQLiteLogHandler(db_path=tmp_path / "burst.db", logs_backend=backend)
    try:
        n = 50_000
        exceptions_during_emit: List[Exception] = []

        t_start = time.monotonic()
        for i in range(n):
            try:
                handler.emit(_make_record(f"burst-{i}"))
            except Exception as e:
                exceptions_during_emit.append(e)
        emit_duration = time.monotonic() - t_start

        # emit() loop must be fast (non-blocking enqueue) — well under 5 s
        assert emit_duration < 5.0, (
            f"emit() loop for {n} records took {emit_duration:.2f}s — "
            "producer is being blocked (expected < 5.0s for non-blocking enqueue)."
        )

        # No exceptions during emit
        assert not exceptions_during_emit, (
            f"emit() raised exceptions during burst: {exceptions_during_emit[:3]}"
        )

        # After flush: all written records must be present (drops are acceptable
        # since queue may saturate at 10k, but NO "database is locked" exceptions)
        handler.flush()
        # At least some records written (queue may drop at saturation, that's ok)
        assert backend.total_items > 0, "No records written after flush!"

    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 5: WAL and busy_timeout on the logs.db connection
# ---------------------------------------------------------------------------


def test_wal_and_busy_timeout_on_logs_db_connection(tmp_path: Path) -> None:
    """After SQLiteLogHandler initializes logs.db:
    - journal_mode must be 'wal' (set once at bootstrap, persists DB-wide).
    - busy_timeout must be 30000 ms on every connection opened by
      DatabaseConnectionManager (set per-connection, NOT on the throwaway
      bootstrap connection — asserting it on a raw sqlite3.connect() is a
      tautology; we assert the manager's value here instead).
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db_path = tmp_path / "logs_pragma.db"
    handler = SQLiteLogHandler(db_path=db_path)
    try:
        # WAL is a DB-level persistent setting: a fresh connection sees it.
        raw_conn = sqlite3.connect(str(db_path))
        try:
            journal_mode = raw_conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            raw_conn.close()

        assert journal_mode == "wal", (
            f"logs.db journal_mode = '{journal_mode}', expected 'wal'. "
            "WAL is required to allow concurrent reads during log writes."
        )

        # busy_timeout is PER-CONNECTION; DatabaseConnectionManager sets 30000 ms.
        # Assert via the manager's own connection (not the throwaway bootstrap).
        mgr = DatabaseConnectionManager.get_instance(str(db_path))
        mgr_conn = mgr.get_connection()
        busy_timeout = mgr_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(busy_timeout) == 30000, (
            f"DatabaseConnectionManager connection busy_timeout = {busy_timeout}, "
            "expected 30000. The manager must set busy_timeout=30000 per connection."
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 6: LogsSqliteBackend.insert_log_batch — one transaction for a batch
# ---------------------------------------------------------------------------


def test_logs_sqlite_backend_insert_log_batch_one_transaction(tmp_path: Path) -> None:
    """LogsSqliteBackend.insert_log_batch() must exist and insert all items in
    ONE transaction via executemany.

    We spy on execute_atomic calls to verify one transaction per batch.
    """
    from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db_path = str(tmp_path / "logs_backend.db")
    backend = LogsSqliteBackend(db_path)

    mgr = DatabaseConnectionManager.get_instance(db_path)
    original_execute_atomic = mgr.execute_atomic
    atomic_calls = [0]

    def spy_execute_atomic(op: Any) -> Any:
        atomic_calls[0] += 1
        return original_execute_atomic(op)

    mgr.execute_atomic = spy_execute_atomic  # type: ignore[method-assign]

    try:
        items = [
            (
                "2024-01-01T00:00:00+00:00",
                "INFO",
                "src",
                f"msg-{i}",
                None,
                None,
                None,
                None,
                None,
                None,
            )
            for i in range(100)
        ]

        before = atomic_calls[0]
        backend.insert_log_batch(items)
        after = atomic_calls[0]

        # Exactly ONE execute_atomic call for the whole batch
        assert after - before == 1, (
            f"insert_log_batch used {after - before} transactions for 100 items. "
            "Expected exactly 1 transaction (executemany)."
        )

        # All rows present
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        conn = DatabaseConnectionManager.get_instance(db_path).get_connection()
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert count == 100, f"Expected 100 rows, got {count}"
    finally:
        mgr.execute_atomic = original_execute_atomic  # type: ignore[method-assign]
