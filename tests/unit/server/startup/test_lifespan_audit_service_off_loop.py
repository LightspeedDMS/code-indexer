"""The lifespan builds its AuditLogService off the event loop.

Construction runs the audit schema upgrade (ALTER TABLE / CREATE INDEX under
an exclusive lock), which can take seconds on a large table.  Invariant: the
lifespan never runs it on the event-loop thread.  Runs the REAL lifespan
(``TestClient(create_app())``) against an isolated data directory.
"""

from __future__ import annotations

import asyncio
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService


def _running_loop_on_this_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.slow
def test_lifespan_constructs_the_audit_service_off_the_event_loop(
    monkeypatch, tmp_path
) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    constructed_on_loop: Dict[int, bool] = {}
    real_init = AuditLogService.__init__

    def recording_init(self, *args, **kwargs) -> None:
        constructed_on_loop[id(self)] = _running_loop_on_this_thread()
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(AuditLogService, "__init__", recording_init)
    app = create_app()
    try:
        with TestClient(app):
            owned = app.state.audit_service
            assert id(owned) in constructed_on_loop
            assert constructed_on_loop[id(owned)] is False
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()


@pytest.mark.slow
def test_lifespan_binds_stops_and_unbinds_the_audit_writer_off_the_event_loop(
    monkeypatch, tmp_path
) -> None:
    """The bind closes a file, stop() joins the writer, the unbind opens one."""
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.audit_logger import password_audit_logger
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    on_loop: Dict[str, bool] = {}
    real_set_audit_service = password_audit_logger.set_audit_service

    def recording_set_audit_service(audit_service) -> None:
        key = "unbind" if audit_service is None else "bind"
        on_loop[key] = _running_loop_on_this_thread()
        real_set_audit_service(audit_service)

    monkeypatch.setattr(
        password_audit_logger, "set_audit_service", recording_set_audit_service
    )
    app = create_app()
    try:
        with TestClient(app):
            owned = app.state.audit_service
            real_stop = owned.stop

            def recording_stop(*args, **kwargs) -> None:
                on_loop["stop"] = _running_loop_on_this_thread()
                real_stop(*args, **kwargs)

            monkeypatch.setattr(owned, "stop", recording_stop)
        assert on_loop == {"bind": False, "stop": False, "unbind": False}
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()
