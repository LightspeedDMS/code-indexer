# ruff: noqa: F811
"""
Route-level coverage tests for admin MCP credential inline routes.

Covers:
1. GET    /api/admin/users/{username}/mcp-credentials
2. POST   /api/admin/users/{username}/mcp-credentials
3. DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}
"""

from unittest.mock import Mock, patch

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401 — imported for pytest fixture discovery
    anon_client,  # noqa: F401
)


# ---------------------------------------------------------------------------
# 1. GET /api/admin/users/{username}/mcp-credentials
# ---------------------------------------------------------------------------


class TestAdminListUserMcpCredentials:
    """GET /api/admin/users/{username}/mcp-credentials"""

    def test_route_registered(self):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "GET"
        )
        assert handler is not None

    def test_requires_admin_auth(self, anon_client):
        response = anon_client.get("/api/admin/users/someuser/mcp-credentials")
        assert response.status_code == 401

    def test_user_not_found_returns_404(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "GET"
        )
        mock_um = Mock()
        mock_um.get_user.return_value = None

        with _patch_closure(handler, "user_manager", mock_um):
            response = admin_client.get("/api/admin/users/ghost/mcp-credentials")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_success_returns_credentials_and_username(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "GET"
        )
        mock_user = Mock()
        mock_user.username = "targetuser"

        mock_um = Mock()
        mock_um.get_user.return_value = mock_user

        with _patch_closure(handler, "user_manager", mock_um):
            with patch(
                "code_indexer.server.auth.mcp_credential_manager"
                ".MCPCredentialManager.get_credentials",
                return_value=[{"credential_id": "cred-1", "name": "My Key"}],
            ):
                response = admin_client.get(
                    "/api/admin/users/targetuser/mcp-credentials"
                )

        assert response.status_code == 200
        data = response.json()
        assert "credentials" in data
        assert data["username"] == "targetuser"


# ---------------------------------------------------------------------------
# 2. POST /api/admin/users/{username}/mcp-credentials
# ---------------------------------------------------------------------------


class TestAdminCreateUserMcpCredential:
    """POST /api/admin/users/{username}/mcp-credentials"""

    def test_route_registered(self):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "POST"
        )
        assert handler is not None

    def test_requires_admin_auth(self, anon_client):
        response = anon_client.post(
            "/api/admin/users/someuser/mcp-credentials", json={}
        )
        assert response.status_code == 401

    def test_user_not_found_returns_404(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "POST"
        )
        mock_um = Mock()
        mock_um.get_user.return_value = None

        with _patch_closure(handler, "user_manager", mock_um):
            response = admin_client.post(
                "/api/admin/users/ghost/mcp-credentials",
                json={"name": "test"},
            )

        assert response.status_code == 404

    def test_success_returns_credential_with_secret(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "POST"
        )
        mock_user = Mock()
        mock_user.username = "targetuser"

        mock_um = Mock()
        mock_um.get_user.return_value = mock_user

        fake_credential = {
            "credential_id": "cred-abc",
            "client_id": "client-xyz",
            "client_secret": "secret-token",
            "name": "My Cred",
            "created_at": "2025-01-01T00:00:00Z",
        }

        with _patch_closure(handler, "user_manager", mock_um):
            with patch(
                "code_indexer.server.auth.mcp_credential_manager"
                ".MCPCredentialManager.generate_credential",
                return_value=fake_credential,
            ):
                response = admin_client.post(
                    "/api/admin/users/targetuser/mcp-credentials",
                    json={"name": "My Cred"},
                )

        assert response.status_code == 201
        data = response.json()
        assert data["credential_id"] == "cred-abc"
        assert "client_secret" in data


# ---------------------------------------------------------------------------
# 3. DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}
# ---------------------------------------------------------------------------


class TestAdminDeleteUserMcpCredential:
    """DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}"""

    def test_route_registered(self):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials/{credential_id}",
            "DELETE",
        )
        assert handler is not None

    def test_requires_admin_auth(self, anon_client):
        response = anon_client.delete(
            "/api/admin/users/someuser/mcp-credentials/cred-1"
        )
        assert response.status_code == 401

    def test_user_not_found_returns_404(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials/{credential_id}",
            "DELETE",
        )
        mock_um = Mock()
        mock_um.get_user.return_value = None

        with _patch_closure(handler, "user_manager", mock_um):
            response = admin_client.delete(
                "/api/admin/users/ghost/mcp-credentials/cred-1"
            )

        assert response.status_code == 404

    def test_credential_not_found_returns_404(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials/{credential_id}",
            "DELETE",
        )
        mock_user = Mock()
        mock_user.username = "targetuser"

        mock_um = Mock()
        mock_um.get_user.return_value = mock_user

        with _patch_closure(handler, "user_manager", mock_um):
            with patch(
                "code_indexer.server.auth.mcp_credential_manager"
                ".MCPCredentialManager.revoke_credential",
                return_value=False,
            ):
                response = admin_client.delete(
                    "/api/admin/users/targetuser/mcp-credentials/nonexistent"
                )

        assert response.status_code == 404

    def test_success_returns_revoked_message(self, admin_client):
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials/{credential_id}",
            "DELETE",
        )
        mock_user = Mock()
        mock_user.username = "targetuser"

        mock_um = Mock()
        mock_um.get_user.return_value = mock_user

        with _patch_closure(handler, "user_manager", mock_um):
            with patch(
                "code_indexer.server.auth.mcp_credential_manager"
                ".MCPCredentialManager.revoke_credential",
                return_value=True,
            ):
                response = admin_client.delete(
                    "/api/admin/users/targetuser/mcp-credentials/cred-1"
                )

        assert response.status_code == 200
        assert "revoked" in response.json()["message"].lower()
