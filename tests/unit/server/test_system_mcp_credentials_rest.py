"""
Unit tests for Story #275: GET /admin/api/system-credentials REST endpoint.

Tests are written FIRST following TDD methodology (red phase).
Uses FastAPI TestClient with the real web router. Minimal patching:
only _require_admin_session and user_manager are replaced.
"""

from datetime import datetime, timezone

import pytest


_FAKE_SYSTEM_CREDS = [
    {
        "credential_id": "sys1",
        "client_id": "cli1",
        "client_id_prefix": "mcp1",
        "name": "cidx-local-auto",
        "created_at": "2024-01-01T00:00:00Z",
        "last_used_at": None,
        "owner": "admin (system)",
        "is_system": True,
    }
]


class TestRestApiSystemCredentials:
    """
    Tests for GET /admin/api/system-credentials REST endpoint.

    Story #275 AC5: Endpoint must return 403/redirect for non-admin and return
    system credentials JSON for admin users.
    """

    def test_endpoint_exists_not_404_without_session(self) -> None:
        """Route must exist (not 404) - it should reject unauthenticated requests."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/admin/api/system-credentials", follow_redirects=False
        )

        assert response.status_code != 404, (
            "Route /admin/api/system-credentials does not exist (404). "
            "The endpoint must be implemented."
        )
        # Without session: reject or redirect
        assert response.status_code in (200, 401, 403, 302), (
            f"Unexpected status without session: {response.status_code}"
        )

    def test_endpoint_returns_200_with_system_credentials_for_admin(self) -> None:
        """Admin session returns 200 JSON with 'system_credentials' list."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.web.auth import SessionData
        from code_indexer.server.auth import dependencies as dep_module
        from code_indexer.server.web import routes as routes_module

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        fake_session = SessionData(
            username="admin",
            role="admin",
            csrf_token="test-csrf-token",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp(),
        )

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return _FAKE_SYSTEM_CREDS

        original_um = dep_module.user_manager
        original_auth = routes_module._require_admin_session
        dep_module.user_manager = FakeUserManager()
        routes_module._require_admin_session = lambda req: fake_session

        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/admin/api/system-credentials")

            assert response.status_code == 200, (
                f"Expected 200, got {response.status_code}: {response.text}"
            )
            data = response.json()
            assert "system_credentials" in data, (
                f"Expected 'system_credentials' key, got: {list(data.keys())}"
            )
            assert isinstance(data["system_credentials"], list)
            assert len(data["system_credentials"]) == 1
        finally:
            dep_module.user_manager = original_um
            routes_module._require_admin_session = original_auth

    def test_endpoint_returns_credential_fields(self) -> None:
        """Response credentials include all required fields from AC2."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.web.auth import SessionData
        from code_indexer.server.auth import dependencies as dep_module
        from code_indexer.server.web import routes as routes_module

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        fake_session = SessionData(
            username="admin",
            role="admin",
            csrf_token="test-csrf-token",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp(),
        )

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return _FAKE_SYSTEM_CREDS

        original_um = dep_module.user_manager
        original_auth = routes_module._require_admin_session
        dep_module.user_manager = FakeUserManager()
        routes_module._require_admin_session = lambda req: fake_session

        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/admin/api/system-credentials")
            data = response.json()

            cred = data["system_credentials"][0]
            required_fields = {
                "credential_id", "client_id_prefix", "name",
                "created_at", "last_used_at", "owner", "is_system",
            }
            missing = required_fields - set(cred.keys())
            assert not missing, f"Missing credential fields in response: {missing}"
            assert cred["is_system"] is True
            assert cred["owner"] == "admin (system)"
        finally:
            dep_module.user_manager = original_um
            routes_module._require_admin_session = original_auth

    def test_endpoint_returns_empty_list_when_no_system_creds(self) -> None:
        """Returns 200 with empty system_credentials list when none exist."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.web.auth import SessionData
        from code_indexer.server.auth import dependencies as dep_module
        from code_indexer.server.web import routes as routes_module

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        fake_session = SessionData(
            username="admin",
            role="admin",
            csrf_token="test-csrf-token",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp(),
        )

        class FakeUserManagerEmpty:
            def get_system_mcp_credentials(self):
                return []

        original_um = dep_module.user_manager
        original_auth = routes_module._require_admin_session
        dep_module.user_manager = FakeUserManagerEmpty()
        routes_module._require_admin_session = lambda req: fake_session

        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/admin/api/system-credentials")

            assert response.status_code == 200
            data = response.json()
            assert data["system_credentials"] == []
        finally:
            dep_module.user_manager = original_um
            routes_module._require_admin_session = original_auth
