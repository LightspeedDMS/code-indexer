"""Audit captures made while the emitters shut down are written, not dropped.

Invariant: the lifespan stops the audit writer and unbinds the audit service
only AFTER the components that emit audit events have stopped, so a capture
made during their shutdown still reaches the store.  Runs the REAL lifespan
(``TestClient(create_app())``) against an isolated data directory.

Accepted residual: ``_mcp_executor.shutdown(wait=False)`` does not wait for
running MCP work, so an MCP handler still running after the unbind can still
record a counted SERVICE_UNRESOLVABLE drop.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import SystemComponent

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"
_TARGET = "shutdown-phase-user"
_MAX_CAPTURES = 2000


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _capture_once() -> None:
    audit_capture.capture_system(
        component=SystemComponent.SELF_REGISTRATION,
        action_type="user_created",
        target_type="user",
        target_id=_TARGET,
        outcome="success",
        details={"role": "normal_user", "provisioning": "self_registration"},
    )


def _rows_for_target(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE target_id = ?", (_TARGET,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


@pytest.mark.slow
def test_shutdown_returns_the_password_audit_logger_to_the_unbound_path(
    monkeypatch, tmp_path
) -> None:
    """No module-level logger keeps a stopped service across lifespans."""
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.audit_logger import password_audit_logger
    from code_indexer.server.services.config_service import reset_config_service

    from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

    # The unbound state before the lifespan: flat-file mode at a path
    # outside the lifespan's data directory.
    pre_boot_dir = tmp_path / "pre-boot"
    pre_boot_dir.mkdir()
    pre_boot_file = pre_boot_dir / "password_audit.log"
    unbound = PasswordChangeAuditLogger(log_file_path=str(pre_boot_file))
    monkeypatch.setattr(password_audit_logger, "_audit_service", None)
    monkeypatch.setattr(password_audit_logger, "audit_logger", unbound.audit_logger)
    monkeypatch.setattr(password_audit_logger, "log_file_path", str(pre_boot_file))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CIDX_DATA_DIR", str(data_dir))
    reset_config_service()
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    app = create_app()
    try:
        with TestClient(app):
            assert password_audit_logger._audit_service is app.state.audit_service
        assert password_audit_logger._audit_service is None
        assert password_audit_logger.log_file_path == str(pre_boot_file)

        password_audit_logger.log_password_change_success("example-user", "192.0.2.1")

        assert audit_capture.records_dropped_since_boot() == 0
        assert "PASSWORD_CHANGE_SUCCESS" in pre_boot_file.read_text()
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()


@pytest.mark.slow
def test_captures_during_emitter_shutdown_are_written_not_unresolvable(
    monkeypatch, tmp_path
) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    collector = _Collector()
    capture_logger = logging.getLogger(CAPTURE_LOGGER)
    capture_logger.addHandler(collector)

    phase_started = threading.Event()
    phase_ended = threading.Event()
    captures = [0]

    def emitter() -> None:
        if not phase_started.wait(120):
            return
        for _ in range(_MAX_CAPTURES):
            _capture_once()
            captures[0] += 1
            if phase_ended.wait(0.005):
                return

    app = create_app()
    thread = threading.Thread(target=emitter, name="shutdown-phase-emitter")
    try:
        with TestClient(app):
            lifecycle = app.state.global_lifecycle_manager
            dep_map = app.state.dependency_map_service
            assert lifecycle is not None and dep_map is not None
            real_lifecycle_stop = lifecycle.stop
            real_dep_map_stop = dep_map.stop_scheduler

            def lifecycle_stop(*args, **kwargs):
                phase_started.set()
                return real_lifecycle_stop(*args, **kwargs)

            def dep_map_stop(*args, **kwargs):
                result = real_dep_map_stop(*args, **kwargs)
                phase_ended.set()
                thread.join(30)
                return result

            monkeypatch.setattr(lifecycle, "stop", lifecycle_stop)
            monkeypatch.setattr(dep_map, "stop_scheduler", dep_map_stop)
            thread.start()
        assert phase_started.is_set() and phase_ended.is_set()
        assert not thread.is_alive()
        assert captures[0] >= 1
        unresolvable = [
            line
            for line in collector.lines
            if line.startswith(audit_capture.SERVICE_UNRESOLVABLE)
        ]
        assert unresolvable == []
        assert audit_capture.records_dropped_since_boot() == 0
        assert _rows_for_target(tmp_path / "groups.db") == captures[0]
    finally:
        phase_started.set()
        phase_ended.set()
        if thread.is_alive():
            thread.join(30)
        capture_logger.removeHandler(collector)
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()
