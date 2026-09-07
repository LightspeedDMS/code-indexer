"""
End-to-end test for JWT token persistence across server restarts.

This test verifies that JWT tokens created by one server instance
remain valid after the server restarts, demonstrating proper
secret key persistence.

Bug #1808: isolation used to be done by patching ``pathlib.Path.home()``.
Production (``JWTSecretManager.__init__``) resolves the server directory as
``os.environ.get("CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server"))``
-- the env var wins over ``Path.home()``. The server test lane MUST set
``CIDX_SERVER_DATA_DIR`` (Bug #1776, to avoid writing into the live dev
server's ``~/.cidx-server/``), which made the ``Path.home()`` patch a no-op
and the file-existence assertions fail. Fixed by isolating via
``monkeypatch.setenv("CIDX_SERVER_DATA_DIR", ...)`` -- the mechanism
production actually honors. When that env var is set, ``JWTSecretManager``
uses the given directory AS the server directory directly (no nested
``.cidx-server`` component), so the secret file is at
``<data_dir>/.jwt_secret``.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app


@pytest.mark.slow
class TestJWTRestartPersistenceE2E:
    """Test JWT token persistence across server restarts end-to-end."""

    def test_jwt_token_survives_server_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that JWT tokens remain valid across server restarts."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))

        # First server instance: log in and confirm the token works.
        client1 = TestClient(create_app())
        login_response = client1.post(
            "/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert login_response.status_code == 200
        access_token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}
        assert client1.get("/api/admin/users", headers=headers).status_code == 200

        # JWT secret file must be created directly under CIDX_SERVER_DATA_DIR.
        secret_file = tmp_path / ".jwt_secret"
        assert secret_file.exists(), "JWT secret file should be created"
        assert len(secret_file.read_text()) > 0, "JWT secret file should not be empty"

        # Simulate a server restart: new app instance reusing the same secret.
        client2 = TestClient(create_app())
        protected_response2 = client2.get("/api/admin/users", headers=headers)
        assert protected_response2.status_code == 200, (
            "Token from first server should work with second server"
        )
        users_data = protected_response2.json()
        assert "users" in users_data
        assert "total" in users_data
        assert users_data["total"] >= 1  # At least admin user should exist

        # Token must keep working for other endpoints too.
        for endpoint in ("/api/admin/users", "/api/admin/golden-repos"):
            response = client2.get(endpoint, headers=headers)
            assert response.status_code != 401, (
                f"Endpoint {endpoint} should not return auth error with valid token"
            )

        # Bug #1808 verification requirement: prove persistence is
        # genuinely discriminating. A third instance backed by a
        # DIFFERENT (fresh, unrelated) data dir has a different secret and
        # must reject the token minted above -- a broken/no-op persistence
        # implementation would make this assertion fail.
        other_dir = tmp_path / "unrelated-data-dir"
        other_dir.mkdir()
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(other_dir))
        client3 = TestClient(create_app())
        cross_dir_response = client3.get("/api/admin/users", headers=headers)
        assert cross_dir_response.status_code == 401, (
            "Token from a server backed by a different data directory "
            "(different JWT secret) must not validate"
        )

    def test_jwt_secret_file_permissions_persist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that JWT secret file maintains proper permissions across restarts."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))

        # Create first server instance
        create_app()

        secret_file = tmp_path / ".jwt_secret"
        assert secret_file.exists()

        # Check initial permissions
        initial_permissions = secret_file.stat().st_mode & 0o777
        assert initial_permissions == 0o600

        # Create second server instance (restart)
        create_app()

        # Permissions should remain the same
        final_permissions = secret_file.stat().st_mode & 0o777
        assert final_permissions == 0o600

        # Secret content should be the same
        assert secret_file.exists()
        assert len(secret_file.read_text()) > 0

    def test_multiple_tokens_persist_across_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that multiple JWT tokens persist across server restart."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))

        # === FIRST SERVER INSTANCE ===
        client1 = TestClient(create_app())

        # Get admin token first
        admin_login = client1.post(
            "/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert admin_login.status_code == 200
        admin_token = admin_login.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # Verify admin token works
        admin_response = client1.get("/api/admin/users", headers=admin_headers)
        assert admin_response.status_code == 200

        # === SERVER RESTART ===
        client2 = TestClient(create_app())

        # Verify admin token still works after restart
        admin_response2 = client2.get("/api/admin/users", headers=admin_headers)
        assert admin_response2.status_code == 200

        # The response should contain the same user data
        users1 = admin_response.json()["users"]
        users2 = admin_response2.json()["users"]

        assert len(users1) == len(users2)
        assert users1[0]["username"] == users2[0]["username"]
        assert users1[0]["role"] == users2[0]["role"]

    def test_jwt_secret_uniqueness_across_different_temp_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Test that different server instances in different directories have different secrets."""
        data_dir1 = tmp_path_factory.mktemp("jwt_dir1")
        data_dir2 = tmp_path_factory.mktemp("jwt_dir2")

        # Create first server in data_dir1
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir1))
        create_app()
        secret_file1 = data_dir1 / ".jwt_secret"
        assert secret_file1.exists()
        secret1 = secret_file1.read_text()

        # Create second server in data_dir2
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir2))
        create_app()
        secret_file2 = data_dir2 / ".jwt_secret"
        assert secret_file2.exists()
        secret2 = secret_file2.read_text()

        # Secrets should be different
        assert secret1 != secret2, (
            "Different server instances should have different secrets"
        )
        assert len(secret1) > 0 and len(secret2) > 0

    def test_environment_variable_override_persists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that environment variable JWT secret is saved and persists."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
        custom_secret = "custom-jwt-secret-from-env-12345"

        # Set environment variable and create first server
        monkeypatch.setenv("JWT_SECRET_KEY", custom_secret)
        create_app()

        # Check that secret was saved to file
        secret_file = tmp_path / ".jwt_secret"
        assert secret_file.exists()
        saved_secret = secret_file.read_text().strip()
        assert saved_secret == custom_secret

        # Create second server without environment variable
        # It should use the saved secret from file
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        create_app()

        # Verify secret is still the same
        assert secret_file.read_text().strip() == custom_secret
