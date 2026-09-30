"""Every audit row the writer does not write at shutdown is counted.

Invariants:

- stop() on a writer that cannot finish (the store is locked by another
  connection) counts every row it did not confirm as written -- the rows
  still queued and the batch the writer holds -- and logs ONE summary ERROR
  line carrying the count, not one line per row.
- An enqueue that races stop() either reaches the writer or is counted; it is
  never left in a queue nobody drains.
- In a server process, legacy log() after the writer stopped is a counted
  drop, never a synchronous write.  The never-marked (CLI) path still writes
  synchronously.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import AuditEvent, build_legacy_event
from code_indexer.server.services.audit_log_service import AuditLogService

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"
# A catalog action type delivered through the async writer (QUEUED).
QUEUED_ACTION_TYPE = "token_refresh_success"


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())


def _event(action_type: str = QUEUED_ACTION_TYPE) -> AuditEvent:
    return build_legacy_event(
        actor="alice",
        action_type=action_type,
        target_type="auth",
        target_id="alice",
        details_json=None,
    )


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])
    finally:
        conn.close()


def _capture_errors(caplog) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno == logging.ERROR
    ]


def test_stop_with_a_blocked_writer_counts_every_unwritten_row_in_one_error_line(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    db_path = tmp_path / "groups.db"
    svc = AuditLogService(db_path)
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    n_rows = 300
    try:
        svc.start()
        for _ in range(n_rows):
            svc.enqueue_event(_event())
        before = audit_capture.records_dropped_since_boot()
        svc.stop(timeout=0.5)
        delta = audit_capture.records_dropped_since_boot() - before
        errors = _capture_errors(caplog)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert delta == n_rows
    assert len(errors) == 1, errors
    assert errors[0].startswith(audit_capture.WRITER_STOPPED)
    assert f"count={n_rows}" in errors[0]


class _WriterKillingService(AuditLogService):
    """Real service whose store write ends the writer thread outright."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.write_entered = threading.Event()
        self.release = threading.Event()

    def insert_events(self, events) -> None:  # type: ignore[override]
        self.write_entered.set()
        self.release.wait(10)
        raise SystemExit("writer thread ends")


# The writer thread ending on an exception IS the scenario under test.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_rows_left_by_a_writer_that_died_are_counted_at_stop(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    svc = _WriterKillingService(tmp_path / "groups.db")
    svc.start()
    thread = svc._writer_thread
    assert thread is not None
    svc.enqueue_event(_event())
    assert svc.write_entered.wait(10)
    n_queued = 5
    for _ in range(n_queued):
        svc.enqueue_event(_event())
    before = audit_capture.records_dropped_since_boot()
    svc.release.set()
    thread.join(10)
    assert not thread.is_alive()

    svc.stop(timeout=1)

    assert audit_capture.records_dropped_since_boot() - before == n_queued + 1
    errors = _capture_errors(caplog)
    assert len(errors) == 1, errors
    assert f"count={n_queued + 1}" in errors[0]


@pytest.mark.parametrize("action_type", [QUEUED_ACTION_TYPE, "user_deleted"])
def test_legacy_log_after_the_writer_stopped_is_a_counted_drop_in_a_server_process(
    tmp_path: Path, caplog, action_type: str
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    db_path = tmp_path / "groups.db"
    svc = AuditLogService(db_path)
    svc.start()
    svc.stop()
    audit_capture.mark_server_process()
    try:
        before = audit_capture.records_dropped_since_boot()
        svc.log("alice", action_type, "user", "bob")
        delta = audit_capture.records_dropped_since_boot() - before
    finally:
        audit_capture.reset_server_process_mark()

    assert _count_rows(db_path) == 0
    assert delta == 1
    errors = _capture_errors(caplog)
    assert len(errors) == 1, errors
    assert errors[0].startswith(audit_capture.WRITER_NOT_RUNNING)


def test_legacy_log_after_the_writer_stopped_writes_synchronously_outside_a_server(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "groups.db"
    svc = AuditLogService(db_path)
    svc.start()
    svc.stop()
    assert not audit_capture.is_server_process()
    before = audit_capture.records_dropped_since_boot()

    svc.log("alice", QUEUED_ACTION_TYPE, "auth", "alice")

    assert _count_rows(db_path) == 1
    assert audit_capture.records_dropped_since_boot() == before


class _PausingQueue(queue.Queue):
    """A real queue whose put pauses until released (forces the interleave)."""

    def __init__(self) -> None:
        super().__init__(maxsize=10)
        self.entered = threading.Event()
        self.release = threading.Event()

    def put_nowait(self, item) -> None:  # type: ignore[override]
        self.entered.set()
        self.release.wait(10)
        super().put_nowait(item)


def test_an_enqueue_racing_stop_is_written_or_counted_never_stranded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "groups.db"
    svc = AuditLogService(db_path)
    svc.start()
    pausing = _PausingQueue()
    svc._queue = pausing
    before = audit_capture.records_dropped_since_boot()

    enqueuer = threading.Thread(target=svc.enqueue_event, args=(_event(),))
    enqueuer.start()
    assert pausing.entered.wait(10)
    stopper = threading.Thread(target=svc.stop, kwargs={"timeout": 10})
    stopper.start()
    # Give stop() every chance to complete while the put is still pending.
    stopper.join(1.5)
    pausing.release.set()
    enqueuer.join(10)
    stopper.join(20)
    assert not stopper.is_alive()

    delta = audit_capture.records_dropped_since_boot() - before
    assert pausing.qsize() == 0
    assert _count_rows(db_path) + delta == 1
