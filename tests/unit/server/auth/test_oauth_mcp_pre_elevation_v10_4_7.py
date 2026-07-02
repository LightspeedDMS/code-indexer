"""v10.4.7 unit tests: OAuth-MCP sessions are pre-elevated in get_mcp_user_from_credentials.

Root cause (Open 1):
  get_mcp_user_from_credentials authenticated OAuth clients but never set
  request.state.user_jti, so the @require_mcp_elevation decorator's Gate 5
  fired with "No session key on MCP request." for every OAuth-MCP call.

Fix (Variant C -- pre-elevated OAuth):
  After successful credential verification:
    1. Set request.state.user_jti = f"oauth:{client_id}"
    2. Call elevated_session_manager.create(session_key=..., username=..., scope="full")

Test patterns follow test_require_elevation.py conventions:
  - MagicMock() for Request (avoids real ASGI scope)
  - Direct module-global assignment with restore in autouse fixture
  - Inline asyncio.get_event_loop().run_until_complete()

Test structure (2 tests per subclass):
  _OAuthPreElevationBase          -- autouse _setup (1 method, no tests)
  TestOAuthJtiSet                 -- 2 tests: jti set, window opened
  TestOAuthElevationErrors        -- 2 tests: bad creds 401, no creds None
  TestOAuthElevationDegradedPaths -- 2 tests: ESM None, create() raises
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
import code_indexer.server.auth.dependencies as _deps

_TEST_CLIENT_ID = "test-client-alpha"
_TEST_USERNAME = "alice"
_EXPECTED_SESSION_KEY = _TEST_CLIENT_ID  # client_id is used directly as session key
_DUMMY_HASH = "not-a-real-hash"
_SECRET_SENTINEL = object()
_HTTP_401 = 401
_CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _make_cred_request() -> MagicMock:
    """Mock POST request with client_secret_post body pre-cached in state._json.

    state.user_jti is seeded to None so tests can assert it was set (or not).
    """
    req = MagicMock()
    req.method = "POST"
    req.headers.get.return_value = ""
    req.state._json = {"client_id": _TEST_CLIENT_ID, "client_secret": _SECRET_SENTINEL}
    req.state.user_jti = None
    return req


def _make_empty_request() -> MagicMock:
    """Mock POST request with no credentials in body or headers."""
    req = MagicMock()
    req.method = "POST"
    req.headers.get.return_value = ""
    req.state._json = {}
    req.state.user_jti = None
    return req


def _make_admin_user() -> User:
    """Admin User with a synthetic non-credential hash."""
    return User(
        username=_TEST_USERNAME,
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
        created_at=_CREATED_AT,
    )


class _OAuthPreElevationBase:
    """Shared setup/restore fixture -- no tests."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> Generator[None, None, None]:
        """Wire module globals; restore originals after each test."""
        self.mock_cred: MagicMock = MagicMock()
        self.mock_cred.verify_credential.return_value = _TEST_USERNAME
        self.mock_um: MagicMock = MagicMock()
        self.mock_um.get_user.return_value = _make_admin_user()
        self.mock_esm: MagicMock = MagicMock()

        orig_cred = _deps.mcp_credential_manager
        orig_um = _deps.user_manager
        orig_esm = _deps.elevated_session_manager

        _deps.mcp_credential_manager = self.mock_cred
        _deps.user_manager = self.mock_um
        _deps.elevated_session_manager = self.mock_esm

        yield

        _deps.mcp_credential_manager = orig_cred
        _deps.user_manager = orig_um
        _deps.elevated_session_manager = orig_esm


class TestOAuthJtiSet(_OAuthPreElevationBase):
    """Tests 1-2: successful OAuth credential_post auth sets user_jti and opens window."""

    def test_oauth_auth_sets_user_jti_to_oauth_prefix(self) -> None:
        """Successful client_secret_post sets request.state.user_jti = f"oauth:{client_id}"."""
        req = _make_cred_request()
        result = asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )

        assert result is not None and result.username == _TEST_USERNAME
        assert req.state.user_jti == _EXPECTED_SESSION_KEY

    def test_oauth_auth_creates_elevation_window(self) -> None:
        """Successful auth calls ESM.create() once with correct session_key, username, scope."""
        req = _make_cred_request()
        asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )

        self.mock_esm.create.assert_called_once()
        pos, kw = self.mock_esm.create.call_args
        combined = dict(
            zip(("session_key", "username", "elevated_from_ip", "scope"), pos)
        )
        combined.update(kw)
        assert combined.get("session_key") == _EXPECTED_SESSION_KEY
        assert combined.get("username") == _TEST_USERNAME
        assert combined.get("scope") == "full"


class TestOAuthElevationErrors(_OAuthPreElevationBase):
    """Tests 3-4: auth failure and absent credentials do not mutate state."""

    def test_oauth_auth_failure_does_not_set_user_jti(self) -> None:
        """verify_credential returns None -> HTTPException 401; user_jti stays None."""
        from fastapi import HTTPException

        self.mock_cred.verify_credential.return_value = None
        req = _make_cred_request()  # state.user_jti seeded to None

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _deps.get_mcp_user_from_credentials(req)
            )

        assert exc_info.value.status_code == _HTTP_401
        assert (
            req.state.user_jti is None
        )  # seeded None; must not be overwritten on failure
        self.mock_esm.create.assert_not_called()

    def test_oauth_no_credentials_returns_none_unchanged(self) -> None:
        """No credentials in body -> returns None; ESM is untouched."""
        req = _make_empty_request()
        result = asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )

        assert result is None
        self.mock_esm.create.assert_not_called()


class TestOAuthElevationDegradedPaths(_OAuthPreElevationBase):
    """Tests 5-6: degraded ESM paths still return User and set user_jti."""

    def test_oauth_pre_elevation_handles_missing_session_manager_gracefully(
        self,
    ) -> None:
        """ESM is None -> user_jti is set, User returned, no exception raised.

        Production code guards with `if elevated_session_manager is not None`
        before calling create(). Session_key is still available to the decorator.
        """
        _deps.elevated_session_manager = None  # type: ignore[assignment]
        req = _make_cred_request()
        result = asyncio.get_event_loop().run_until_complete(
            _deps.get_mcp_user_from_credentials(req)
        )

        assert result is not None and result.username == _TEST_USERNAME
        assert req.state.user_jti == _EXPECTED_SESSION_KEY

    def test_oauth_pre_elevation_handles_create_exception_gracefully(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """create() raises -> User returned, user_jti set, WARNING logged."""
        self.mock_esm.create.side_effect = RuntimeError("DB connection lost")
        req = _make_cred_request()

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.auth.dependencies"
        ):
            result = asyncio.get_event_loop().run_until_complete(
                _deps.get_mcp_user_from_credentials(req)
            )

        assert result is not None and result.username == _TEST_USERNAME
        assert req.state.user_jti == _EXPECTED_SESSION_KEY
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "pre-elevate" in str(m) or "oauth" in str(m).lower()
            for m in warning_messages
        ), f"Expected a pre-elevation warning. Logged: {warning_messages!r}"
