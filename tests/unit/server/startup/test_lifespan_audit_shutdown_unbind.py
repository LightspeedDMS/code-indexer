"""Shutdown always unbinds the audit service, even when its stop raises.

Invariant: a failing writer stop never leaves the process-wide audit binding
pointing at a stopped service.  Runs the REAL lifespan
(``TestClient(create_app())``) against an isolated data directory.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services import audit_capture


@pytest.mark.slow
def test_shutdown_unbinds_the_audit_service_even_when_its_stop_raises(
    monkeypatch, tmp_path
) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    app = create_app()
    owned = None
    try:
        with TestClient(app):
            owned = app.state.audit_service
            assert audit_capture.resolve_audit_sink("before-shutdown") is owned
            real_stop = owned.stop

            def failing_stop(*args, **kwargs) -> None:
                real_stop(*args, **kwargs)
                raise RuntimeError("writer stop failed")

            monkeypatch.setattr(owned, "stop", failing_stop)
        with pytest.raises(audit_capture.AuditServiceUnresolvable):
            audit_capture.resolve_audit_sink("after-shutdown")
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        reset_config_service()
