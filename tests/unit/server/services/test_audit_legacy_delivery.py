"""Legacy entry points (``log`` / ``log_raw``) use the catalog's delivery.

A started service delivers each legacy row the way its action type's catalog
entry says: DURABLE rows are committed on the caller's thread before the call
returns; QUEUED rows go to the writer thread, and a full queue is a counted
drop with no write on the caller's thread.  A service that was never started
keeps writing synchronously (tests and pre-start callers).

Every test uses a REAL AuditLogService on a real temporary SQLite file.
"""

from __future__ import annotations

import ast
import inspect
import logging
import sqlite3
import threading
import warnings
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    build_legacy_event,
)

from _audit_capture_support import ThreadRecordingAuditLogService, rows

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "groups.db"


@pytest.fixture()
def started(db_path: Path):
    service = ThreadRecordingAuditLogService(db_path)
    service.start()
    try:
        yield service
    finally:
        service.stop()


def test_durable_legacy_row_is_written_before_log_returns(started, db_path) -> None:
    started.log(
        admin_id="admin",
        action_type="group_create",
        target_type="group",
        target_id="7",
        details='{"group_name": "example"}',
    )
    # No flush: a DURABLE row is committed before log() returns.
    assert [r[0] for r in rows(db_path)] == ["group_create"]
    assert started.insert_threads == [threading.get_ident()]


def test_queued_legacy_row_is_written_by_the_writer_thread(started, db_path) -> None:
    started.log(
        admin_id="alice",
        action_type="token_refresh_success",
        target_type="auth",
        target_id="alice",
    )
    started.flush()
    assert [r[0] for r in rows(db_path)] == ["token_refresh_success"]
    assert threading.get_ident() not in started.insert_threads


def test_log_raw_uses_the_same_delivery(started, db_path) -> None:
    started.log_raw(
        timestamp="2026-01-01T00:00:00+00:00",
        admin_id="alice",
        action_type="password_change_success",
        target_type="auth",
        target_id="alice",
    )
    assert [r[0] for r in rows(db_path)] == ["password_change_success"]
    assert started.insert_threads == [threading.get_ident()]


def test_full_queue_is_a_counted_drop_with_no_caller_write(
    started, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    blocker = sqlite3.connect(str(db_path), timeout=30)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        filler = build_legacy_event(
            actor="filler",
            action_type="token_refresh_success",
            target_type="auth",
            target_id="filler",
            details_json=None,
        )
        while not started._queue.full():
            started._queue.put_nowait(filler)
        before = audit_capture.records_dropped_since_boot()
        started.log(
            admin_id="dropped",
            action_type="token_refresh_success",
            target_type="auth",
            target_id="dropped",
        )
        assert audit_capture.records_dropped_since_boot() == before + 1
        assert threading.get_ident() not in started.insert_threads
    finally:
        blocker.rollback()
        blocker.close()
    started.flush()
    assert rows(db_path, "target_id = ?", ("dropped",)) == []
    assert any(
        r.getMessage().startswith(audit_capture.QUEUE_SATURATED)
        for r in caplog.records
        if r.name == CAPTURE_LOGGER
    )


def test_uncatalogued_legacy_type_is_kept_and_written_durably(started, db_path) -> None:
    started.log_raw(
        timestamp="2020-01-01T00:00:00+00:00",
        admin_id="alice",
        action_type="historic_flat_file_type",
        target_type="auth",
        target_id="alice",
    )
    assert [r[0] for r in rows(db_path)] == ["historic_flat_file_type"]
    assert started.insert_threads == [threading.get_ident()]
    assert audit_capture.records_dropped_since_boot() == 0


def test_never_started_service_writes_synchronously(db_path) -> None:
    service = ThreadRecordingAuditLogService(db_path)
    service.log(
        admin_id="alice",
        action_type="token_refresh_success",
        target_type="auth",
        target_id="alice",
    )
    assert [r[0] for r in rows(db_path)] == ["token_refresh_success"]
    assert service.insert_threads == [threading.get_ident()]


_SERVER_SRC = Path(audit_capture.__file__).resolve().parents[1]
_WRITER_CALLS = {"log_audit", "log", "log_raw", "ensure_user_group_membership"}


def _literal_action_types() -> Dict[str, str]:
    """Every action-type literal a server writer passes today -> one site."""
    found: Dict[str, str] = {}
    for path in sorted(_SERVER_SRC.rglob("*.py")):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # unrelated source-level warnings
            tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values: List[ast.expr] = []
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name in _WRITER_CALLS:
                    values = [k.value for k in node.keywords if k.arg == "action_type"]
            elif isinstance(node, ast.Dict):
                values = [
                    v
                    for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and k.value == "event_type"
                ]
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    line = getattr(node, "lineno", 0)
                    found.setdefault(value.value, f"{path.name}:{line}")
    return found


def test_every_literal_legacy_action_type_is_catalogued() -> None:
    from code_indexer.server.services import group_access_manager

    found = _literal_action_types()
    default = (
        inspect.signature(
            group_access_manager.GroupAccessManager.ensure_user_group_membership
        )
        .parameters["action_type"]
        .default
    )
    found.setdefault(default, "ensure_user_group_membership default")
    assert len(found) >= 20  # the scan really saw the writers
    missing = {a: site for a, site in found.items() if a not in AUDIT_ACTION_CATALOG}
    assert missing == {}


@pytest.mark.asyncio
async def test_durable_legacy_row_on_the_event_loop_goes_to_the_writer(
    started, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    started.log(
        admin_id="admin",
        action_type="group_delete",
        target_type="group",
        target_id="7",
    )
    assert threading.get_ident() not in started.insert_threads
    started.flush()
    assert [r[0] for r in rows(db_path)] == ["group_delete"]
    assert any(
        r.getMessage().startswith(audit_capture.ON_EVENT_LOOP)
        for r in caplog.records
        if r.name == CAPTURE_LOGGER
    )
