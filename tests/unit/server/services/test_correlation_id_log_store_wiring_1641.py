"""Bug #1641: correlation_id reader is fixed (#1631/#1632) but the log
store's correlation_id column stays null for ~97% of entries.

Root cause: #1631/#1632 fixed get_correlation_id() to correctly read the
REGISTERED CorrelationBridgeMiddleware's ContextVar. But SQLiteLogHandler
(the single handler that persists rows queried by admin_logs_query) only
ever reads `record.correlation_id` -- an attribute that exists on a
LogRecord ONLY when the CALL SITE explicitly built
`extra={"correlation_id": ...}` (e.g. via logging_utils.get_log_extra(), or
inline as middleware/sanitization.py's CSRF logging does). The vast
majority of `logger.info()/warning()/error()` calls across the codebase
pass no `extra` at all, so the record never carries the attribute --
regardless of how correctly the reader itself is wired.

Compounding this: in production, SQLiteLogHandler is ALWAYS installed
behind async_logging.install_queue_logging()'s QueueListener (Bug #1078).
SQLiteLogHandler.emit() therefore executes on the LISTENER thread, not the
original request thread -- so even a fallback read of get_correlation_id()
placed inside SQLiteLogHandler.emit() itself would see None: contextvars do
not propagate across a plain threading.Thread boundary. The correlation id
must be captured BEFORE the record crosses into the queue, on the request
thread that still has the contextvar populated.

Fix: IdentityQueueHandler.prepare() (async_logging.py) -- which runs
synchronously on the original calling/request thread, before enqueue --
injects `record.correlation_id` from the SAME correctly-wired
get_correlation_id() reader whenever the record doesn't already carry one.
This is a single central wiring point (no per-call-site changes). A
defense-in-depth fallback is also added inside SQLiteLogHandler.emit()
itself for the (non-production) direct-attached-without-queue case.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from code_indexer.server.telemetry.correlation_bridge import (
    _correlation_id_var,
    set_current_correlation_id,
)


@pytest.fixture(autouse=True)
def _reset_correlation_contextvar():
    """Prevent correlation-id leakage between tests via the shared ContextVar."""
    token = _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


class TestPlainLogCallPersistsCorrelationIdViaAsyncQueue:
    """Real production path: root logger -> IdentityQueueHandler ->
    DrainableQueueListener -> SQLiteLogHandler, exactly as wired by
    startup/lifespan.py's install_queue_logging(...) call."""

    def test_plain_logger_call_with_active_correlation_id_persists_column(
        self, tmp_path
    ) -> None:
        from code_indexer.server.services.async_logging import install_queue_logging
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)
            listener = install_queue_logging([sqlite_handler])
            try:
                set_current_correlation_id("real-request-id-1641")

                # Plain call -- NO extra={"correlation_id": ...}. This is
                # what the vast majority of call sites across the codebase
                # do (confirmed: only ~12 files use get_log_extra()/an
                # explicit correlation_id extra out of hundreds of files
                # calling logger.info/warning/error).
                logging.getLogger("test.bug1641").info("a perfectly ordinary log line")

                listener.flush()  # drains listener + SQLiteLogHandler writer queue
            finally:
                listener.stop()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT correlation_id FROM logs WHERE message LIKE "
                    "'%a perfectly ordinary log line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, "log row was not persisted at all"
            assert row[0] == "real-request-id-1641", (
                "SQLiteLogHandler persisted correlation_id="
                f"{row[0]!r} for a plain logger.info() call made inside an "
                "active correlation-id context -- the column must be "
                "populated via the central async-queue wiring point even "
                "when the call site passes no "
                "extra={'correlation_id': ...}."
            )
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()

    def test_explicit_extra_correlation_id_is_not_overridden(self, tmp_path) -> None:
        """A call site that already set an explicit correlation_id (e.g. one
        propagated from a different request/context than the current one)
        must NOT be clobbered by the central injection."""
        from code_indexer.server.services.async_logging import install_queue_logging
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)
            listener = install_queue_logging([sqlite_handler])
            try:
                set_current_correlation_id("ambient-id-should-be-ignored")

                logging.getLogger("test.bug1641").info(
                    "explicit correlation id line",
                    extra={"correlation_id": "explicit-id-1641"},
                )

                listener.flush()
            finally:
                listener.stop()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT correlation_id FROM logs WHERE message LIKE "
                    "'%explicit correlation id line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None
            assert row[0] == "explicit-id-1641", (
                "an explicitly-provided extra={'correlation_id': ...} must "
                f"win over the ambient context value; got {row[0]!r}"
            )
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()

    def test_no_active_correlation_id_leaves_column_null(self, tmp_path) -> None:
        """No active correlation id in context -> column stays NULL (never
        fabricate a value)."""
        from code_indexer.server.services.async_logging import install_queue_logging
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)
            listener = install_queue_logging([sqlite_handler])
            try:
                # No set_current_correlation_id() call -- context is empty.
                logging.getLogger("test.bug1641").info("no correlation context line")
                listener.flush()
            finally:
                listener.stop()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT correlation_id FROM logs WHERE message LIKE "
                    "'%no correlation context line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None
            assert row[0] is None
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()


class TestIdentityQueueHandlerPrepareInjectsCorrelationId:
    """Unit-level proof at the exact wiring point (runs on the calling
    thread, before the record crosses into the async queue)."""

    def test_prepare_injects_correlation_id_when_missing(self) -> None:
        import queue

        from code_indexer.server.services.async_logging import IdentityQueueHandler

        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)
        record = logging.LogRecord(
            name="test.bug1641.unit",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="no extra passed",
            args=(),
            exc_info=None,
        )
        assert not hasattr(record, "correlation_id")

        set_current_correlation_id("unit-level-id-1641")
        prepared = handler.prepare(record)

        assert prepared is record, "prepare() must still return the same object"
        assert getattr(prepared, "correlation_id", None) == "unit-level-id-1641"

    def test_prepare_does_not_override_existing_correlation_id(self) -> None:
        import queue

        from code_indexer.server.services.async_logging import IdentityQueueHandler

        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)
        record = logging.LogRecord(
            name="test.bug1641.unit",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="explicit extra passed",
            args=(),
            exc_info=None,
        )
        record.correlation_id = "already-set-id"

        set_current_correlation_id("ambient-id-should-not-win")
        prepared = handler.prepare(record)

        assert prepared.correlation_id == "already-set-id"

    def test_prepare_does_not_set_attribute_when_no_active_correlation_id(
        self,
    ) -> None:
        import queue

        from code_indexer.server.services.async_logging import IdentityQueueHandler

        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)
        record = logging.LogRecord(
            name="test.bug1641.unit",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="no context",
            args=(),
            exc_info=None,
        )

        prepared = handler.prepare(record)

        assert getattr(prepared, "correlation_id", None) is None


class TestSqliteLogHandlerDirectAttachFallback:
    """Defense-in-depth: SQLiteLogHandler attached directly to a logger
    WITHOUT the async queue in between (e.g. a non-standard deployment or a
    test harness) must still populate correlation_id, since in that
    configuration emit() runs on the original calling thread."""

    def test_direct_attach_without_queue_still_persists_correlation_id(
        self, tmp_path
    ) -> None:
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs_direct.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)

            set_current_correlation_id("direct-attach-id-1641")
            logging.getLogger("test.bug1641.direct").info("direct attach line")
            sqlite_handler.flush()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT correlation_id FROM logs WHERE message LIKE "
                    "'%direct attach line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None
            assert row[0] == "direct-attach-id-1641"
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()
