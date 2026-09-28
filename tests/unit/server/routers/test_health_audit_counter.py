"""The dropped-audit-record counter is visible on every health surface.

A real drop is produced (a marked server process with no audit service
bound -- the wiring-defect path), then the counter is read back through
``HealthCheckService.get_system_health()``, the REST ``GET /health`` front
door (TestClient over the real app) and the MCP ``check_health`` handler.
A drop never changes the health status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services import audit_capture

HEALTH_PATH = "/health"


def _admin() -> User:
    from datetime import datetime, timezone

    return User(
        username="health-admin",
        password_hash="unused-hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


def _produce_one_drop() -> None:
    """A marked process with nothing bound: a counted drop (wiring defect)."""
    audit_capture.clear_audit_service()
    audit_capture.mark_server_process()
    audit_capture.capture(
        actor="health-admin",
        action_type="user_email_changed",
        target_type="user",
        target_id="someone",
        outcome="success",
    )


def _reset_process_binding() -> None:
    # Process wiring left behind by an earlier test in the same session (an
    # app build marks the process; a bind sets the node id) must not leak in.
    audit_capture.clear_audit_service()
    audit_capture.reset_server_process_mark()


@pytest.fixture(autouse=True)
def isolated_binding(monkeypatch) -> Iterator[None]:
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    _reset_process_binding()
    yield
    _reset_process_binding()


def _health_service(tmp_path: Path):
    from code_indexer.server.services.health_service import HealthCheckService

    service = HealthCheckService()
    service.database_url = f"sqlite:///{tmp_path / 'cidx_server.db'}"
    return service


def test_get_system_health_reports_counter_and_node_id(tmp_path: Path) -> None:
    service = _health_service(tmp_path)
    first = service.get_system_health()
    assert first.audit is not None
    assert first.audit.records_dropped_since_boot == 0
    assert first.audit.node_id is None
    _produce_one_drop()
    second = service.get_system_health()
    assert second.audit is not None
    assert second.audit.records_dropped_since_boot == 1
    assert not any("audit" in reason.lower() for reason in second.failure_reasons)


def _rest_health(client: TestClient) -> Dict[str, Any]:
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200
    body: Dict[str, Any] = response.json()
    return body


def test_rest_health_front_door_reports_the_counter() -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.dependencies import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(app, raise_server_exceptions=True)
        assert _rest_health(client)["audit"] == {
            "records_dropped_since_boot": 0,
            "node_id": None,
        }
        _produce_one_drop()
        _produce_one_drop()
        assert _rest_health(client)["audit"]["records_dropped_since_boot"] == 2
    finally:
        app.dependency_overrides.clear()


def test_mcp_check_health_reports_the_counter() -> None:
    from code_indexer.server.mcp.handlers.repos import check_health

    _produce_one_drop()
    result = check_health({}, _admin())
    payload = result["content"][0]["text"]
    import json

    body = json.loads(payload)
    assert body["success"] is True
    assert body["health"]["audit"] == {
        "records_dropped_since_boot": 1,
        "node_id": None,
    }
