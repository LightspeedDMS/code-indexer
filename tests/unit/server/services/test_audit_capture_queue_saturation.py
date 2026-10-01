"""QUEUED delivery: O(1) on the caller's thread; saturation is a counted drop.

The writer is held back for real by an EXCLUSIVE lock taken on the same
SQLite file through a separate connection, so the queue fills up exactly as
it would behind a stalled store.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import build_legacy_event
from code_indexer.server.services.audit_log_service import _AUDIT_QUEUE_MAXSIZE

from _audit_capture_support import (
    ThreadRecordingAuditLogService,
    bind_service,
    bound_started_service,
    rows,
    unbind,
)

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())


def _queued_event(target: str = "alice"):
    return build_legacy_event(
        actor=target,
        action_type="token_refresh_success",
        target_type="auth",
        target_id=target,
        details_json=None,
    )


def test_queued_event_is_written_by_the_writer_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    for service in bound_started_service(db_path):
        audit_capture.record(_queued_event())
        service.flush()
        assert [r[0] for r in rows(db_path)] == ["token_refresh_success"]
        assert threading.get_ident() not in service.insert_threads
        assert len(service.insert_threads) == 1


def test_full_queue_is_a_counted_drop_without_a_caller_write(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    db_path = tmp_path / "groups.db"
    for service in bound_started_service(db_path):
        blocker = sqlite3.connect(str(db_path), timeout=30)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            filler = _queued_event("filler")
            # The writer may already hold one batch; fill what is left.
            while not service._queue.full():
                service._queue.put_nowait(filler)
            before = audit_capture.records_dropped_since_boot()
            audit_capture.record(_queued_event("dropped"))
            assert audit_capture.records_dropped_since_boot() == before + 1
            assert threading.get_ident() not in service.insert_threads
        finally:
            blocker.rollback()
            blocker.close()
        service.flush()
        assert rows(db_path, "target_id = ?", ("dropped",)) == []
        assert len(rows(db_path)) >= _AUDIT_QUEUE_MAXSIZE
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno == logging.ERROR
    ]
    assert len(lines) == 1
    assert lines[0].startswith(audit_capture.QUEUE_SATURATED)


def test_writer_not_running_is_a_counted_drop(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    db_path = tmp_path / "groups.db"
    service = ThreadRecordingAuditLogService(db_path)  # never started
    bind_service(service)
    try:
        before = audit_capture.records_dropped_since_boot()
        audit_capture.record(_queued_event())
        assert audit_capture.records_dropped_since_boot() == before + 1
        assert service.insert_threads == []
    finally:
        unbind(service)
    assert rows(db_path) == []
    assert any(
        r.getMessage().startswith(audit_capture.WRITER_NOT_RUNNING)
        for r in caplog.records
        if r.name == CAPTURE_LOGGER
    )
