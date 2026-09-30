"""The process-wide audit binding contract.

- A process that never marked itself as a server (standalone CLI) has no
  store: capture is a silent no-op.
- A marked server process with nothing bound is a wiring defect: every
  capture is a counted drop with an ERROR naming AuditServiceUnresolvable.
- A bound process writes to the ONE bound service.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService

from _audit_capture_support import bind_service, rows, unbind

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@pytest.fixture(autouse=True)
def clean_binding(monkeypatch):
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    unbind()
    yield
    unbind()


def _capture() -> None:
    audit_capture.capture(
        actor="alice",
        action_type="user_email_changed",
        target_type="user",
        target_id="bob",
        outcome="success",
    )


def _errors(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno == logging.ERROR
    ]


def test_unmarked_process_is_a_silent_noop(caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    assert audit_capture.resolve_audit_sink("test") is None
    _capture()
    assert audit_capture.records_dropped_since_boot() == 0
    assert _errors(caplog) == []


def test_marked_process_without_binding_is_a_counted_error(caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    audit_capture.mark_server_process()
    with pytest.raises(audit_capture.AuditServiceUnresolvable):
        audit_capture.resolve_audit_sink("test")
    _capture()
    assert audit_capture.records_dropped_since_boot() == 1
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "AuditServiceUnresolvable" in errors[0]
    assert "action_type=user_email_changed" in errors[0]


def test_bound_process_writes_to_the_bound_service(tmp_path: Path) -> None:
    db_path = tmp_path / "groups.db"
    service = AuditLogService(db_path)
    bind_service(service, node_id="node-b")
    assert audit_capture.resolve_audit_sink("test") is service
    assert audit_capture.audit_node_id() == "node-b"
    _capture()
    found = rows(db_path)
    assert [(r[0], r[5]) for r in found] == [("user_email_changed", "node-b")]
    assert audit_capture.records_dropped_since_boot() == 0


def test_clear_unbinds_and_resets_node_id(tmp_path: Path) -> None:
    bind_service(AuditLogService(tmp_path / "groups.db"), node_id="node-b")
    audit_capture.clear_audit_service()
    assert audit_capture.audit_node_id() is None
    with pytest.raises(audit_capture.AuditServiceUnresolvable):
        audit_capture.resolve_audit_sink("test")


def test_empty_node_id_is_stored_as_none(tmp_path: Path) -> None:
    bind_service(AuditLogService(tmp_path / "groups.db"), node_id="")
    assert audit_capture.audit_node_id() is None


def test_bind_rejects_none() -> None:
    with pytest.raises(ValueError):
        audit_capture.bind_audit_service(None, node_id=None)  # type: ignore[arg-type]
