"""Tests for SSO password change protection (Bug #68).

Security Vulnerability: SSO users (authenticated via OIDC) could have their passwords
changed, allowing them to bypass SSO and authenticate locally. This defeats the
security purpose of SSO.

These tests verify:
1. is_sso_user() correctly identifies SSO users (users with oidc_identity)
2. change_password() raises SSOPasswordChangeError for SSO users
3. Regular users can still change passwords (regression test)
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from code_indexer.server.auth.rate_limiter import password_change_rate_limiter
from code_indexer.server.auth.user_manager import (
    UserManager,
    UserRole,
    SSOPasswordChangeError,
)


class TestIsSsoUser:
    """Tests for is_sso_user() helper method."""

    def test_is_sso_user_returns_true_for_oidc_user(self):
        """
        Given a user with oidc_identity set
        When is_sso_user() is called
        Then it returns True.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Create a user and set OIDC identity
            manager.create_user("ssouser", "SecurePass123!@#", UserRole.NORMAL_USER)
            manager.set_oidc_identity(
                "ssouser",
                {
                    "subject": "oidc-12345",
                    "email": "sso@example.com",
                    "linked_at": "2025-01-15T10:30:00Z",
                },
            )

            # Verify is_sso_user returns True
            assert manager.is_sso_user("ssouser") is True

    def test_is_sso_user_returns_false_for_regular_user(self):
        """
        Given a regular user without oidc_identity
        When is_sso_user() is called
        Then it returns False.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Create a regular user (no OIDC identity)
            manager.create_user("regularuser", "SecurePass123!@#", UserRole.NORMAL_USER)

            # Verify is_sso_user returns False
            assert manager.is_sso_user("regularuser") is False

    def test_is_sso_user_returns_false_for_nonexistent_user(self):
        """
        Given a nonexistent username
        When is_sso_user() is called
        Then it returns False.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Verify is_sso_user returns False for nonexistent user
            assert manager.is_sso_user("nonexistent") is False

    def test_is_sso_user_returns_false_after_oidc_identity_removed(self):
        """
        Given a user who had OIDC identity that was later removed
        When is_sso_user() is called
        Then it returns False.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Create user with OIDC identity
            manager.create_user("exssouser", "SecurePass123!@#", UserRole.NORMAL_USER)
            manager.set_oidc_identity(
                "exssouser",
                {"subject": "oidc-12345", "email": "ex@example.com"},
            )

            # Verify user is SSO
            assert manager.is_sso_user("exssouser") is True

            # Remove OIDC identity
            manager.remove_oidc_identity("exssouser")

            # Verify user is no longer SSO
            assert manager.is_sso_user("exssouser") is False


class TestIsSsoUserSQLite:
    """Tests for is_sso_user() with SQLite backend."""

    @pytest.fixture
    def sqlite_db_path(self, tmp_path: Path) -> str:
        """Create and initialize a SQLite database for testing."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()
        return str(db_path)

    def test_is_sso_user_sqlite_returns_true_for_oidc_user(
        self, sqlite_db_path: str
    ) -> None:
        """
        Given a UserManager in SQLite mode with a user with oidc_identity
        When is_sso_user() is called
        Then it returns True.
        """
        manager = UserManager(use_sqlite=True, db_path=sqlite_db_path)

        # Create user and set OIDC identity
        manager.create_user("sqlitesso", "SecurePass123!@#", UserRole.NORMAL_USER)
        manager.set_oidc_identity(
            "sqlitesso",
            {"subject": "oidc-sqlite-123", "email": "sqlite@example.com"},
        )

        assert manager.is_sso_user("sqlitesso") is True

    def test_is_sso_user_sqlite_returns_false_for_regular_user(
        self, sqlite_db_path: str
    ) -> None:
        """
        Given a UserManager in SQLite mode with a regular user
        When is_sso_user() is called
        Then it returns False.
        """
        manager = UserManager(use_sqlite=True, db_path=sqlite_db_path)

        # Create regular user
        manager.create_user("sqliteregular", "SecurePass123!@#", UserRole.NORMAL_USER)

        assert manager.is_sso_user("sqliteregular") is False


class TestChangePasswordSsoProtection:
    """Tests for change_password() SSO protection."""

    def test_change_password_raises_for_sso_user(self):
        """
        Given an SSO user (has oidc_identity)
        When change_password() is called
        Then it raises SSOPasswordChangeError.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Create SSO user
            manager.create_user("ssouser", "SecurePass123!@#", UserRole.NORMAL_USER)
            manager.set_oidc_identity(
                "ssouser",
                {"subject": "oidc-12345", "email": "sso@example.com"},
            )

            # Attempt to change password should raise SSOPasswordChangeError
            with pytest.raises(SSOPasswordChangeError) as exc_info:
                manager.change_password("ssouser", "NewSecurePass456!@#")

            # Verify error message
            assert "SSO users" in str(exc_info.value)
            assert "identity provider" in str(exc_info.value)

    def test_change_password_succeeds_for_regular_user(self):
        """
        Given a regular user (no oidc_identity)
        When change_password() is called
        Then it succeeds (regression test).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # Create regular user
            manager.create_user("regularuser", "SecurePass123!@#", UserRole.NORMAL_USER)

            # Change password should succeed
            result = manager.change_password("regularuser", "NewSecurePass456!@#")
            assert result is True

            # Verify new password works
            user = manager.authenticate_user("regularuser", "NewSecurePass456!@#")
            assert user is not None

    def test_change_password_returns_false_for_nonexistent_user(self):
        """
        Given a nonexistent username
        When change_password() is called
        Then it returns False (existing behavior, not SSOPasswordChangeError).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            manager = UserManager(users_file_path=users_file)

            # change_password returns False for nonexistent user (not raises)
            result = manager.change_password("nonexistent", "NewSecurePass456!@#")
            assert result is False


class TestChangePasswordSsoProtectionSQLite:
    """Tests for change_password() SSO protection with SQLite backend."""

    @pytest.fixture
    def sqlite_db_path(self, tmp_path: Path) -> str:
        """Create and initialize a SQLite database for testing."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()
        return str(db_path)

    def test_change_password_sqlite_raises_for_sso_user(
        self, sqlite_db_path: str
    ) -> None:
        """
        Given an SSO user in SQLite mode
        When change_password() is called
        Then it raises SSOPasswordChangeError.
        """
        manager = UserManager(use_sqlite=True, db_path=sqlite_db_path)

        # Create SSO user
        manager.create_user("sqlitesso", "SecurePass123!@#", UserRole.NORMAL_USER)
        manager.set_oidc_identity(
            "sqlitesso",
            {"subject": "oidc-sqlite-123", "email": "sqlite@example.com"},
        )

        # Attempt to change password should raise
        with pytest.raises(SSOPasswordChangeError) as exc_info:
            manager.change_password("sqlitesso", "NewSecurePass456!@#")

        assert "SSO users" in str(exc_info.value)

    def test_change_password_sqlite_succeeds_for_regular_user(
        self, sqlite_db_path: str
    ) -> None:
        """
        Given a regular user in SQLite mode
        When change_password() is called
        Then it succeeds.
        """
        manager = UserManager(use_sqlite=True, db_path=sqlite_db_path)

        # Create regular user
        manager.create_user("sqliteregular", "SecurePass123!@#", UserRole.NORMAL_USER)

        # Change password should succeed
        result = manager.change_password("sqliteregular", "NewSecurePass456!@#")
        assert result is True


class TestSSOPasswordChangeErrorException:
    """Tests for SSOPasswordChangeError exception class."""

    def test_sso_password_change_error_is_exception(self):
        """
        Given SSOPasswordChangeError
        When raised
        Then it is catchable as an Exception.
        """
        with pytest.raises(Exception):
            raise SSOPasswordChangeError("test message")

    def test_sso_password_change_error_preserves_message(self):
        """
        Given SSOPasswordChangeError with a message
        When str() is called
        Then it contains the message.
        """
        error = SSOPasswordChangeError("Cannot change password for SSO users")
        assert "Cannot change password for SSO users" in str(error)


# Password constants to avoid magic strings
SSO_USER_PASSWORD = "TempPass123!@#"
REGULAR_USER_PASSWORD = "RegularPass123!@#"


@pytest.mark.e2e
class TestSSOPasswordChangeAPIProtection:
    """API endpoint tests for SSO password change protection (Bug #68)."""

    @pytest.fixture(autouse=True)
    def reset_rate_limiter(self):
        """Reset rate limiter state between tests."""
        password_change_rate_limiter._attempts.clear()
        yield
        password_change_rate_limiter._attempts.clear()

    @pytest.fixture
    def real_app(self, tmp_path):
        """Create real FastAPI app with test database."""
        temp_dir = str(tmp_path / "server_data")
        os.makedirs(temp_dir, exist_ok=True)
        users_file = Path(temp_dir) / "users.json"
        users_file.write_text("{}")

        old_env = os.environ.get("CIDX_SERVER_DATA_DIR")
        os.environ["CIDX_SERVER_DATA_DIR"] = temp_dir

        try:
            app = create_app()
            yield app
        finally:
            if old_env is not None:
                os.environ["CIDX_SERVER_DATA_DIR"] = old_env
            else:
                os.environ.pop("CIDX_SERVER_DATA_DIR", None)
            shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def client(self, real_app):
        """Create test client with real app."""
        return TestClient(real_app)

    @pytest.fixture
    def sso_user(self, real_app):
        """Create an SSO user (user with OIDC identity)."""
        from code_indexer.server.auth import dependencies

        user_manager = dependencies.user_manager
        user_manager.create_user(
            username="ssouser", password=SSO_USER_PASSWORD, role=UserRole.NORMAL_USER
        )
        user_manager.set_oidc_identity(
            "ssouser",
            {"subject": "oidc-api-test-123", "email": "sso@example.com"},
        )
        return user_manager.get_user("ssouser")

    @pytest.fixture
    def regular_user(self, real_app):
        """Create a regular user (no OIDC identity)."""
        from code_indexer.server.auth import dependencies

        user_manager = dependencies.user_manager
        user_manager.create_user(
            username="regularuser", password=REGULAR_USER_PASSWORD, role=UserRole.NORMAL_USER
        )
        return user_manager.get_user("regularuser")

    @pytest.fixture
    def admin_user(self, real_app):
        """Create an admin user for admin endpoint tests."""
        from code_indexer.server.auth import dependencies

        user_manager = dependencies.user_manager
        existing = user_manager.get_user("admin")
        if existing:
            return existing
        return None  # Use seeded admin

    @pytest.fixture
    def sso_user_auth_headers(self, client, sso_user):
        """Get authentication headers for SSO user."""
        response = client.post(
            "/auth/login", json={"username": "ssouser", "password": SSO_USER_PASSWORD}
        )
        assert response.status_code == 200, f"Login failed: {response.text}"
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    @pytest.fixture
    def admin_auth_headers(self, client, admin_user):
        """Get authentication headers for admin user."""
        response = client.post(
            "/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert response.status_code == 200, f"Admin login failed: {response.text}"
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_self_password_change_returns_403_for_sso_user(
        self, client, sso_user_auth_headers
    ):
        """SSO user attempting to change own password gets HTTP 403."""
        response = client.put(
            "/api/users/change-password",
            headers=sso_user_auth_headers,
            json={"old_password": SSO_USER_PASSWORD, "new_password": "NewPass456!@#"},
        )
        assert response.status_code == 403
        assert "SSO" in response.json()["detail"]

    def test_admin_password_change_returns_403_for_sso_user(
        self, client, admin_auth_headers, sso_user
    ):
        """Admin attempting to change SSO user's password gets HTTP 403."""
        response = client.put(
            "/api/admin/users/ssouser/change-password",
            headers=admin_auth_headers,
            json={"old_password": "dummy", "new_password": "NewPass456!@#"},
        )
        assert response.status_code == 403
        assert "SSO" in response.json()["detail"]

    def test_web_route_handler_catches_sso_password_change_error(self):
        """
        Unit test: Web route handler correctly catches SSOPasswordChangeError.

        This tests that the web route has the proper exception handling in place
        for SSO users, without requiring a full web session setup.
        """
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.routes import change_user_password
        from code_indexer.server.web.auth import SessionData

        # Create mock request
        mock_request = MagicMock()
        mock_request.cookies = {"session": "valid_session"}

        # Create admin session
        admin_session = SessionData(
            username="admin",
            role="admin",
            csrf_token="test_csrf",
            created_at=1234567890.0,
        )

        # Mock _require_admin_session to return admin session
        with patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=admin_session,
        ):
            # Mock CSRF validation to pass
            with patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ):
                # Mock user_manager to raise SSOPasswordChangeError
                mock_user_manager = MagicMock()
                mock_user_manager.change_password.side_effect = SSOPasswordChangeError(
                    "Cannot change password for SSO users."
                )

                with patch(
                    "code_indexer.server.auth.dependencies.user_manager",
                    mock_user_manager,
                ):
                    # Mock _create_users_page_response to capture the call
                    with patch(
                        "code_indexer.server.web.routes._create_users_page_response"
                    ) as mock_response:
                        mock_response.return_value = MagicMock()

                        # Call the web route handler
                        change_user_password(
                            request=mock_request,
                            username="ssouser",
                            new_password="NewPass456!@#",
                            confirm_password="NewPass456!@#",
                            csrf_token="test_csrf",
                        )

                        # Verify _create_users_page_response was called with error message
                        mock_response.assert_called_once()
                        call_kwargs = mock_response.call_args[1]
                        assert "error_message" in call_kwargs
                        error_msg = call_kwargs["error_message"]
                        assert "SSO" in error_msg or "identity provider" in error_msg
