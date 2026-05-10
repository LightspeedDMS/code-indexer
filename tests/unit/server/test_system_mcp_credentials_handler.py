"""
Unit tests for Story #275: handle_admin_list_system_mcp_credentials() behaviour.

Migrated in Story #989 to use the new unified list_mcp_credentials(scope='system')
handler instead of the removed handle_admin_list_system_mcp_credentials handler.

Tests are written following TDD methodology.
Minimal patching: only user_manager is replaced with a test double.
"""

import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

# ---------------------------------------------------------------------------
# Elevation bypass helpers (mirrors test_mcp_credentials_unified.py)
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
_TEST_SESSION_KEY = "test-session-system-cred-handler-abc"
_IDLE_SECONDS = 300
_MAX_AGE_SECONDS = 1800
_DB_FILENAME = "elev_system_cred.db"
_ELEV_SCOPE = "full"


@contextlib.contextmanager
def _active_elevation(username: str, tmp_path):
    """Open a real elevation window so decorated handlers pass the gate."""
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_SECONDS,
        max_age_seconds=_MAX_AGE_SECONDS,
        db_path=str(tmp_path / _DB_FILENAME),
    )
    mgr.create(_TEST_SESSION_KEY, username, None, scope=_ELEV_SCOPE)
    totp_mock = MagicMock()
    totp_mock.is_mfa_enabled.return_value = True
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, mgr),
        patch(_TOTP_PATH, return_value=totp_mock),
    ):
        yield _TEST_SESSION_KEY


def _make_admin_user():
    """Create a User with admin role for handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="hash",
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_normal_user():
    """Create a User with normal_user role for handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="alice",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


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


class TestHandleAdminListSystemMcpCredentials:
    """
    Tests for list_mcp_credentials(scope='system') behaviour.

    Story #275 AC4: Handler must require admin role, return system credentials
    with is_system=True, and follow existing _mcp_response handler conventions.

    Story #989: handler now accessed via list_mcp_credentials(scope='system')
    which routes to the _list_system inner handler (elevation required).
    """

    def test_returns_permission_denied_for_non_admin(self, tmp_path) -> None:
        """Non-admin user receives success=False with permission error."""
        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with _active_elevation(_make_normal_user().username, tmp_path):
            result = handler(
                {"scope": "system"},
                _make_normal_user(),
                session_key=_TEST_SESSION_KEY,
            )

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False
        error_lower = content["error"].lower()
        assert (
            "permission" in error_lower
            or "denied" in error_lower
            or "admin" in error_lower
        )

    def test_returns_permission_denied_for_power_user(self, tmp_path) -> None:
        """Power user also receives success=False (not admin)."""
        from code_indexer.server.auth.user_manager import User, UserRole

        power_user = User(
            username="bob",
            password_hash="hash",
            role=UserRole.POWER_USER,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with _active_elevation(power_user.username, tmp_path):
            result = handler(
                {"scope": "system"},
                power_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False

    def test_returns_system_credentials_for_admin(self, tmp_path) -> None:
        """Admin receives success=True with system_credentials list."""
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return _FAKE_SYSTEM_CREDS

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManager()
        try:
            handler = HANDLER_REGISTRY["list_mcp_credentials"]
            with _active_elevation(_make_admin_user().username, tmp_path):
                result = handler(
                    {"scope": "system"},
                    _make_admin_user(),
                    session_key=_TEST_SESSION_KEY,
                )
            content = json.loads(result["content"][0]["text"])

            assert content["success"] is True
            assert "system_credentials" in content
            assert content["count"] == 1
            assert content["system_credentials"][0]["is_system"] is True
            assert content["system_credentials"][0]["name"] == "cidx-local-auto"
        finally:
            dep_module.user_manager = original

    def test_returns_empty_list_when_no_system_credentials(self, tmp_path) -> None:
        """Admin receives success=True with empty list when no system creds exist."""
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManagerEmpty:
            def get_system_mcp_credentials(self):
                return []

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManagerEmpty()
        try:
            handler = HANDLER_REGISTRY["list_mcp_credentials"]
            with _active_elevation(_make_admin_user().username, tmp_path):
                result = handler(
                    {"scope": "system"},
                    _make_admin_user(),
                    session_key=_TEST_SESSION_KEY,
                )
            content = json.loads(result["content"][0]["text"])

            assert content["success"] is True
            assert content["system_credentials"] == []
            assert content["count"] == 0
        finally:
            dep_module.user_manager = original

    def test_response_is_mcp_compliant_content_array(self, tmp_path) -> None:
        """Response must wrap data in MCP content array (content[0].type='text')."""
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return []

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManager()
        try:
            handler = HANDLER_REGISTRY["list_mcp_credentials"]
            with _active_elevation(_make_admin_user().username, tmp_path):
                result = handler(
                    {"scope": "system"},
                    _make_admin_user(),
                    session_key=_TEST_SESSION_KEY,
                )

            assert "content" in result, "Response must have 'content' key"
            assert isinstance(result["content"], list)
            assert len(result["content"]) == 1
            assert result["content"][0]["type"] == "text"
            # text must be valid JSON
            json.loads(result["content"][0]["text"])
        finally:
            dep_module.user_manager = original

    def test_handler_is_registered_in_handler_registry(self) -> None:
        """HANDLER_REGISTRY must contain 'list_mcp_credentials' (unified handler)."""
        assert "list_mcp_credentials" in HANDLER_REGISTRY, (
            "Handler 'list_mcp_credentials' not found in HANDLER_REGISTRY"
        )
