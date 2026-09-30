"""Fail-open: an audit write failure never blocks the action.

The store is made unwritable for real (the table is renamed through a
separate connection), never mocked.  Each test gets a fresh drop reporter so
the 60-second log rate-limit windows of one test cannot hide another's line.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import build_legacy_event

from _audit_capture_support import bound_started_service

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"
SECRET_DETAIL = "s3cr3t-detail-value"
TARGET_VALUE = "target-bob"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    reporter = audit_capture._DropReporter()
    monkeypatch.setattr(audit_capture, "_reporter", reporter)
    return reporter


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "groups.db"


@pytest.fixture()
def service(db_path: Path):
    yield from bound_started_service(db_path)


def _break_store(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("ALTER TABLE audit_logs RENAME TO audit_logs_moved")
        conn.commit()
    finally:
        conn.close()


def _errors(caplog) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno == logging.ERROR
    ]


def _delete_user_action() -> str:
    """Stand-in for an audited action: it must complete whatever the audit does."""
    audit_capture.capture(
        actor="alice",
        action_type="user_deleted",
        target_type="user",
        target_id=TARGET_VALUE,
        outcome="success",
        details={"deleted_role": "normal_user"},
    )
    return "deleted"


def test_durable_write_failure_lets_the_action_proceed(
    service, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    _break_store(db_path)
    before = audit_capture.records_dropped_since_boot()
    assert _delete_user_action() == "deleted"
    assert audit_capture.records_dropped_since_boot() == before + 1
    errors = _errors(caplog)
    assert len(errors) == 1
    line = errors[0].getMessage()
    assert line.startswith(audit_capture.WRITE_FAILED)
    assert "action_type=user_deleted" in line
    assert "error_class=OperationalError" in line
    assert "event_uuid=" in line and "event_uuid=None" not in line
    assert "correlation_id=evt-" in line
    assert "no such table" not in line
    assert TARGET_VALUE not in line
    assert "normal_user" not in line


def test_repeated_failures_are_counted_but_folded(service, db_path, caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    _break_store(db_path)
    before = audit_capture.records_dropped_since_boot()
    for _ in range(5):
        _delete_user_action()
    assert audit_capture.records_dropped_since_boot() == before + 5
    assert len(_errors(caplog)) == 1


def test_writer_thread_drop_is_counted_without_the_message(
    service, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    _break_store(db_path)
    before = audit_capture.records_dropped_since_boot()
    service.log(
        admin_id="admin",
        action_type="group_create",
        target_type="group",
        target_id=TARGET_VALUE,
        details=f'{{"secret": "{SECRET_DETAIL}"}}',
    )
    service.flush()
    assert audit_capture.records_dropped_since_boot() == before + 1
    line = _errors(caplog)[0].getMessage()
    assert "action_type=group_create" in line
    assert SECRET_DETAIL not in line
    assert "no such table" not in line


def test_invalid_details_are_dropped_and_name_only_the_field(
    service, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    before = audit_capture.records_dropped_since_boot()
    audit_capture.capture(
        actor="alice",
        action_type="user_deleted",
        target_type="user",
        target_id="bob",
        outcome="success",
        details={"password": SECRET_DETAIL},
    )
    assert audit_capture.records_dropped_since_boot() == before + 1
    line = _errors(caplog)[0].getMessage()
    assert line.startswith(audit_capture.EVENT_REJECTED)
    assert "field=password" in line
    assert SECRET_DETAIL not in line


def test_record_of_an_uncatalogued_type_is_a_counted_drop(service, caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    before = audit_capture.records_dropped_since_boot()
    audit_capture.record(
        build_legacy_event(
            actor="admin",
            action_type="not_in_catalog",
            target_type="user",
            target_id="bob",
            details_json=None,
        )
    )
    assert audit_capture.records_dropped_since_boot() == before + 1
    assert "field=action_type" in _errors(caplog)[0].getMessage()


def test_rate_limit_window_folds_then_reports_suppressed_count(caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    clock = FakeClock()
    reporter = audit_capture._DropReporter(clock=clock)
    for _ in range(4):
        reporter.report("situation-x", action_type="user_deleted")
    assert len(_errors(caplog)) == 1
    clock.now += audit_capture.DROP_LOG_WINDOW_SECONDS
    reporter.report("situation-x", action_type="user_deleted")
    lines = [r.getMessage() for r in _errors(caplog)]
    assert len(lines) == 2
    assert lines[1].endswith("suppressed_since_last=3")
    assert reporter.dropped == 5


def test_rate_limit_is_per_situation_and_action_type(caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    reporter = audit_capture._DropReporter(clock=FakeClock())
    reporter.report("situation-x", action_type="user_deleted")
    reporter.report("situation-x", action_type="api_key_created")
    reporter.report("situation-y", action_type="user_deleted")
    assert len(_errors(caplog)) == 3


def test_uncounted_report_does_not_move_the_counter() -> None:
    reporter = audit_capture._DropReporter(clock=FakeClock())
    reporter.report("situation-x", action_type="user_deleted", counted=False)
    assert reporter.dropped == 0


def test_rate_limit_state_stays_bounded() -> None:
    clock = FakeClock()
    reporter = audit_capture._DropReporter(clock=clock)
    for i in range(audit_capture._MAX_RATE_LIMIT_KEYS + 50):
        reporter.report("situation-x", action_type=f"type-{i}")
    assert len(reporter._windows) <= audit_capture._MAX_RATE_LIMIT_KEYS
    assert reporter.dropped == audit_capture._MAX_RATE_LIMIT_KEYS + 50
