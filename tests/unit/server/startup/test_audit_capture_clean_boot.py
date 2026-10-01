"""Clean boot: nothing audited runs between the server mark and the bind.

Runs the REAL lifespan (``TestClient(create_app())`` as a context manager)
against the isolated ``CIDX_SERVER_DATA_DIR`` the slow lane provides.  After
startup the bound sink is the lifespan-owned service, no record was dropped,
no capture ERROR was logged, and shutdown unbinds the service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services import audit_capture

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.mark.slow
def test_boot_migrates_a_flat_audit_file_without_loop_guard_errors(
    monkeypatch, tmp_path
) -> None:
    import sqlite3

    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    # A current timestamp: boot-time retention cleanup removes old rows.
    now = datetime.now(timezone.utc).isoformat()
    (tmp_path / "password_audit.log").write_text(
        "2026-01-01 00:00:00 UTC - INFO - PASSWORD_CHANGE_SUCCESS: "
        '{"event_type": "password_change_success", "username": "alice", '
        f'"timestamp": "{now}"}}\n'
    )
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    collector = _Collector()
    capture_logger = logging.getLogger(CAPTURE_LOGGER)
    capture_logger.addHandler(collector)
    try:
        with TestClient(create_app()):
            pass
        conn = sqlite3.connect(str(tmp_path / "groups.db"))
        try:
            migrated = conn.execute(
                "SELECT admin_id, source FROM audit_logs "
                "WHERE action_type = 'password_change_success'"
            ).fetchall()
        finally:
            conn.close()
        assert migrated == [("alice", "system")]
        assert collector.lines == []
        assert audit_capture.records_dropped_since_boot() == 0
    finally:
        capture_logger.removeHandler(collector)
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()


@pytest.mark.slow
def test_real_boot_binds_the_service_with_zero_drops(monkeypatch) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.auth.user_manager import User, UserRole

    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    collector = _Collector()
    capture_logger = logging.getLogger(CAPTURE_LOGGER)
    capture_logger.addHandler(collector)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: User(
        username="boot-admin",
        password_hash="unused-hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )
    try:
        with TestClient(app) as client:
            sink = audit_capture.resolve_audit_sink("clean-boot")
            assert sink is app.state.audit_service
            assert audit_capture.records_dropped_since_boot() == 0
            health = client.get("/health").json()
            assert health["audit"]["records_dropped_since_boot"] == 0
        with pytest.raises(audit_capture.AuditServiceUnresolvable):
            audit_capture.resolve_audit_sink("after-shutdown")
        assert collector.lines == []
    finally:
        capture_logger.removeHandler(collector)
        app.dependency_overrides.clear()
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
