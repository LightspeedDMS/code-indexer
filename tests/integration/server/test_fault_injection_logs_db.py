"""
Story #746 M6 — logs.db boundary test for fault injection events.

Verifies that FaultInjectionService.record_injection() persists a log entry
to a real SQLite database via the standard SQLiteLogHandler logging pipeline.

Requirements validated:
  1. A real SQLiteLogHandler is attached to the 'fault_injection' logger.
  2. Calling svc.record_injection() emits exactly one log entry.
  3. The entry's source column matches 'fault_injection'.
  4. The entry's message contains target, fault_type, and correlation_id.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler


@pytest.fixture()
def logs_db(tmp_path: Path) -> Path:
    """Return path to a temp SQLite DB file (not yet created)."""
    return tmp_path / "test_fault_injection_logs.db"


@pytest.fixture()
def handler_and_logger(logs_db: Path):
    """
    Attach a SQLiteLogHandler to the 'fault_injection' logger.

    Yields (handler, logger).  Removes the handler on teardown so it does
    not leak into other tests in the session.
    """
    fi_logger = logging.getLogger("fault_injection")
    original_level = fi_logger.level
    original_propagate = fi_logger.propagate

    handler = SQLiteLogHandler(db_path=logs_db)
    handler.setLevel(logging.DEBUG)
    fi_logger.addHandler(handler)
    fi_logger.setLevel(logging.DEBUG)
    # Do not propagate to root logger to avoid noise in the test output.
    fi_logger.propagate = False

    yield handler, fi_logger

    fi_logger.removeHandler(handler)
    handler.close()
    fi_logger.setLevel(original_level)
    fi_logger.propagate = original_propagate


class TestFaultInjectionLogsDbBoundary:
    """M6: fault injection events are persisted to the real SQLite logs DB."""

    def test_record_injection_persists_to_sqlite(
        self,
        logs_db: Path,
        handler_and_logger: tuple,
    ) -> None:
        """
        One record_injection() call produces exactly one row in the logs DB.

        The row must:
          - have source == 'fault_injection'
          - contain target string in message
          - contain fault_type string in message
          - contain correlation_id string in message
        """
        _handler, _logger = handler_and_logger

        svc = FaultInjectionService()
        target = "api.voyageai.com"
        fault_type = "http_error"
        correlation_id = str(uuid.uuid4())

        svc.record_injection(
            target=target,
            fault_type=fault_type,
            correlation_id=correlation_id,
        )

        conn = sqlite3.connect(str(logs_db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT source, message FROM logs WHERE source='fault_injection'"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, (
            f"Expected exactly 1 log row for source='fault_injection', "
            f"got {len(rows)}: {[dict(r) for r in rows]}"
        )
        message = rows[0]["message"]
        assert target in message, (
            f"Expected target {target!r} in log message, got: {message!r}"
        )
        assert fault_type in message, (
            f"Expected fault_type {fault_type!r} in log message, got: {message!r}"
        )
        assert correlation_id in message, (
            f"Expected correlation_id {correlation_id!r} in log message, "
            f"got: {message!r}"
        )
