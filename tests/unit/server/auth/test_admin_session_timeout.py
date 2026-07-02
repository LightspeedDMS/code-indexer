"""
Tests for Story #564: Shorter Admin Session Timeout.

Admin sessions expire after 1 hour (configurable) instead of 8 hours.
Non-admin roles keep 8-hour default. Timeout determined at login time by role.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock

from itsdangerous import URLSafeTimedSerializer

from code_indexer.server.web.auth import SessionManager, SESSION_COOKIE_NAME

DEFAULT_SESSION_TIMEOUT = 28800  # 8 hours
ADMIN_SESSION_TIMEOUT = 3600  # 1 hour


@dataclass
class FakeServerConfig:
    """Minimal server config for testing."""

    host: str = "127.0.0.1"


@dataclass
class FakeWebSecurityConfig:
    """Minimal web security config for testing."""

    web_session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT
    admin_session_timeout_seconds: int = ADMIN_SESSION_TIMEOUT


class TestWebSecurityConfigAdminTimeout:
    """Test that WebSecurityConfig has admin_session_timeout_seconds field."""

    def test_default_admin_session_timeout_is_3600(self):
        """Admin session timeout defaults to 3600 seconds (1 hour)."""
        from code_indexer.server.utils.config_manager import WebSecurityConfig

        config = WebSecurityConfig()
        assert config.admin_session_timeout_seconds == ADMIN_SESSION_TIMEOUT

    def test_custom_admin_session_timeout(self):
        """Admin session timeout can be set to custom value."""
        from code_indexer.server.utils.config_manager import WebSecurityConfig

        config = WebSecurityConfig(admin_session_timeout_seconds=7200)
        assert config.admin_session_timeout_seconds == 7200

    def test_default_web_session_timeout_unchanged(self):
        """Non-admin session timeout remains 28800 seconds (8 hours)."""
        from code_indexer.server.utils.config_manager import WebSecurityConfig

        config = WebSecurityConfig()
        assert config.web_session_timeout_seconds == DEFAULT_SESSION_TIMEOUT


class TestSessionManagerAdminTimeout:
    """Test that SessionManager uses role-appropriate timeouts."""

    def _make_manager(self, web_security_config=None):
        """Create a SessionManager with optional web security config."""
        server_config = FakeServerConfig()
        if web_security_config is None:
            web_security_config = FakeWebSecurityConfig()
        return SessionManager(
            secret_key="test-secret-key",
            config=server_config,
            web_security_config=web_security_config,
        )

    def test_admin_session_cookie_uses_admin_timeout(self):
        """Admin role sessions set cookie max_age to admin_session_timeout_seconds."""
        manager = self._make_manager()
        response = MagicMock()
        response.set_cookie = MagicMock()

        manager.create_session(response, username="admin", role="admin")

        call_kwargs = response.set_cookie.call_args
        assert call_kwargs is not None
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs["max_age"] == ADMIN_SESSION_TIMEOUT
        else:
            assert call_kwargs[1]["max_age"] == ADMIN_SESSION_TIMEOUT

    def test_nonadmin_session_cookie_uses_default_timeout(self):
        """Non-admin role sessions set cookie max_age to web_session_timeout_seconds."""
        manager = self._make_manager()
        response = MagicMock()
        response.set_cookie = MagicMock()

        manager.create_session(response, username="viewer", role="viewer")

        call_kwargs = response.set_cookie.call_args
        assert call_kwargs is not None
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs["max_age"] == DEFAULT_SESSION_TIMEOUT
        else:
            assert call_kwargs[1]["max_age"] == DEFAULT_SESSION_TIMEOUT

    def test_admin_session_custom_timeout(self):
        """Admin timeout respects custom configuration value."""
        web_config = FakeWebSecurityConfig(admin_session_timeout_seconds=1800)
        manager = self._make_manager(web_security_config=web_config)
        response = MagicMock()
        response.set_cookie = MagicMock()

        manager.create_session(response, username="admin", role="admin")

        call_kwargs = response.set_cookie.call_args
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs["max_age"] == 1800
        else:
            assert call_kwargs[1]["max_age"] == 1800

    def test_session_data_includes_timeout(self):
        """Session data includes the timeout used, for validation on read."""
        manager = self._make_manager()
        response = MagicMock()
        response.set_cookie = MagicMock()

        manager.create_session(response, username="admin", role="admin")

        call_args = response.set_cookie.call_args
        if call_args.kwargs:
            signed_value = call_args.kwargs["value"]
        else:
            signed_value = call_args[0][1]

        serializer = URLSafeTimedSerializer("test-secret-key")
        data = serializer.loads(signed_value, salt="web-session")
        assert data["session_timeout"] == ADMIN_SESSION_TIMEOUT

    def test_get_session_works_for_admin(self):
        """get_session returns valid session data for admin sessions."""
        manager = self._make_manager()

        response = MagicMock()
        response.set_cookie = MagicMock()
        manager.create_session(response, username="admin", role="admin")

        call_args = response.set_cookie.call_args
        if call_args.kwargs:
            signed_value = call_args.kwargs["value"]
        else:
            signed_value = call_args[0][1]

        request = MagicMock()
        request.cookies = {SESSION_COOKIE_NAME: signed_value}

        session = manager.get_session(request)
        assert session is not None
        assert session.username == "admin"
        assert session.role == "admin"

    def test_get_session_works_for_nonadmin(self):
        """get_session returns valid session data for non-admin sessions."""
        manager = self._make_manager()

        response = MagicMock()
        response.set_cookie = MagicMock()
        manager.create_session(response, username="viewer", role="viewer")

        call_args = response.set_cookie.call_args
        if call_args.kwargs:
            signed_value = call_args.kwargs["value"]
        else:
            signed_value = call_args[0][1]

        request = MagicMock()
        request.cookies = {SESSION_COOKIE_NAME: signed_value}

        session = manager.get_session(request)
        assert session is not None
        assert session.username == "viewer"
        assert session.role == "viewer"

    def test_init_session_manager_accepts_web_security_config(self):
        """init_session_manager accepts web_security_config parameter."""
        from code_indexer.server.web.auth import init_session_manager

        web_config = FakeWebSecurityConfig()
        server_config = FakeServerConfig()
        manager = init_session_manager(
            secret_key="test-key",
            config=server_config,
            web_security_config=web_config,
        )
        assert manager is not None
