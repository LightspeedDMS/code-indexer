"""Per-request audit attribution context (source, peer address, auth method)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
    classify_source,
    current_audit_request_context,
)


@pytest.mark.parametrize(
    "path, source",
    [
        ("/mcp", "mcp"),
        ("/mcp-public", "mcp"),
        ("/admin/users", "web"),
        ("/user/api-keys", "web"),
        ("/login", "web"),
        ("/login/sso", "web"),
        ("/api/v1/groups", "rest"),
        ("/auth/login", "rest"),
        ("/health", "rest"),
        ("/administrator", "rest"),
    ],
)
def test_source_classification(path: str, source: str) -> None:
    assert classify_source(path) == source


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)

    def _auth_dependency() -> None:
        # Sync dependencies run in a worker thread with a COPIED context:
        # they mutate the holder, never re-set the var.
        ctx = current_audit_request_context()
        assert ctx is not None
        if ctx.auth_method is None:
            ctx.auth_method = "jwt"

    def _snapshot() -> Dict[str, Optional[Any]]:
        ctx = current_audit_request_context()
        if ctx is None:
            return {"bound": False}
        return {
            "bound": True,
            "source": ctx.source,
            "client_ip": ctx.client_ip,
            "auth_method": ctx.auth_method,
        }

    @app.get("/api/probe", dependencies=[Depends(_auth_dependency)])
    def api_probe() -> Dict[str, Optional[Any]]:
        return _snapshot()

    @app.get("/admin/probe")
    def admin_probe() -> Dict[str, Optional[Any]]:
        return _snapshot()

    @app.get("/mcp/probe")
    async def mcp_probe() -> Dict[str, Optional[Any]]:
        return _snapshot()

    return app


def test_rest_request_is_bound_and_dependency_mutation_is_visible() -> None:
    body = TestClient(_probe_app()).get("/api/probe").json()
    assert body == {
        "bound": True,
        "source": "rest",
        "client_ip": "testclient",
        "auth_method": "jwt",
    }


def test_web_request_presets_web_session() -> None:
    body = TestClient(_probe_app()).get("/admin/probe").json()
    assert body["source"] == "web"
    assert body["auth_method"] == "web_session"


def test_mcp_request_is_classified_mcp() -> None:
    body = TestClient(_probe_app()).get("/mcp/probe").json()
    assert body["source"] == "mcp"
    assert body["auth_method"] is None


def test_context_is_reset_after_the_request() -> None:
    TestClient(_probe_app()).get("/api/probe")
    assert current_audit_request_context() is None


def test_create_app_registers_middleware_and_marks_server_process() -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services import audit_capture

    audit_capture.reset_server_process_mark()
    try:
        app = create_app()
        middleware_classes = [m.cls for m in app.user_middleware]
        assert AuditRequestContextMiddleware in middleware_classes
        with pytest.raises(audit_capture.AuditServiceUnresolvable):
            audit_capture.resolve_audit_sink("test")
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
