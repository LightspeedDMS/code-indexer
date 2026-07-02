"""
Unit tests: Bearer token path in _hybrid_auth_impl sets request.state.user_jti.

Root cause: Before the fix, the Bearer token path returned current_user without
setting request.state.user_jti, so _resolve_session_key always returned None for
JWT Bearer token clients, making elevation windows invisible to them.

AC1: Bearer token path sets request.state.user_jti from JWT jti claim.
AC2: Bearer token with no jti claim does NOT set request.state.user_jti.
AC3: Bearer token where jwt_manager.validate_token raises InvalidTokenError or
     TokenExpiredError does NOT set user_jti and does not propagate the exception.
AC4: Session cookie path continues to set user_jti from the cookie value
     (unchanged behavior).
"""

import types
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock, patch

from fastapi.security import HTTPAuthorizationCredentials

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import _hybrid_auth_impl
from code_indexer.server.auth.jwt_manager import InvalidTokenError, TokenExpiredError


# ---------------------------------------------------------------------------
# Named constants - deliberately non-credential-shaped placeholders
# ---------------------------------------------------------------------------
_TEST_JTI = "test-jti-fixture"
_TEST_TOKEN = "test-bearer-token-fixture"
_USERNAME = "api_user"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    """Create a real User instance with a non-secret placeholder hash."""
    return User(
        username=_USERNAME,
        password_hash="placeholder-hash-not-real",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


def _make_credentials(token: str = _TEST_TOKEN) -> MagicMock:
    """Create HTTPAuthorizationCredentials with a non-secret placeholder token."""
    creds = MagicMock(spec=HTTPAuthorizationCredentials)
    creds.credentials = token
    return creds


def _make_request_no_session() -> MagicMock:
    """Create a mock Request with no session cookie.

    Uses types.SimpleNamespace for request.state so hasattr checks for
    attribute presence/absence are reliable (MagicMock auto-creates
    attributes on access, making absence assertions unreliable).
    """
    mock_request = MagicMock()
    mock_request.cookies = {}
    mock_request.state = types.SimpleNamespace()
    return mock_request


def _run_bearer_auth(
    validate_token_result: Optional[Dict[str, Any]] = None,
    validate_token_raises: Optional[Exception] = None,
) -> Tuple[Any, MagicMock, MagicMock]:
    """Run _hybrid_auth_impl through the Bearer token path.

    Patches get_current_user, jwt_manager, and get_session_manager so
    the function takes the Bearer (not session-cookie) code path.

    Args:
        validate_token_result: Payload dict returned by jwt_manager.validate_token.
            Pass None to have it raise instead.
        validate_token_raises: Exception raised by jwt_manager.validate_token.
            Must be InvalidTokenError or TokenExpiredError (the exceptions
            validate_token actually raises for non-JWT / expired tokens).

    Returns:
        (result_user, request, mock_jwt_manager)
    """
    user = _make_user()
    request = _make_request_no_session()
    credentials = _make_credentials()

    mock_jwt_manager = MagicMock()
    if validate_token_raises is not None:
        mock_jwt_manager.validate_token.side_effect = validate_token_raises
    else:
        mock_jwt_manager.validate_token.return_value = validate_token_result or {}

    with patch(
        "code_indexer.server.auth.dependencies.get_current_user",
        return_value=user,
    ):
        with patch(
            "code_indexer.server.auth.dependencies.jwt_manager",
            mock_jwt_manager,
        ):
            with patch("code_indexer.server.web.auth.get_session_manager") as mock_gsm:
                mock_gsm.return_value.get_session.return_value = None
                result = _hybrid_auth_impl(
                    request=request,
                    credentials=credentials,
                    require_admin=False,
                )

    return result, request, mock_jwt_manager


# ---------------------------------------------------------------------------
# AC1 + AC2: Bearer path JTI presence cases
# ---------------------------------------------------------------------------


class TestBearerJtiPresent:
    """Bearer token path sets user_jti when JWT payload contains a jti claim."""

    def test_ac1_sets_user_jti_from_payload_jti_claim(self):
        """AC1: user_jti is set to the jti value from the decoded JWT payload."""
        result, request, mock_jwt = _run_bearer_auth(
            validate_token_result={"jti": _TEST_JTI, "username": _USERNAME}
        )

        assert result.username == _USERNAME
        assert request.state.user_jti == _TEST_JTI
        mock_jwt.validate_token.assert_called_once_with(_TEST_TOKEN)

    def test_ac2_no_jti_in_payload_leaves_user_jti_unset(self):
        """AC2: When jti is absent from payload, user_jti is NOT set on state."""
        result, request, _ = _run_bearer_auth(
            validate_token_result={"username": _USERNAME}
        )

        assert result.username == _USERNAME
        assert not hasattr(request.state, "user_jti")


# ---------------------------------------------------------------------------
# AC3: Bearer path when validate_token raises
# ---------------------------------------------------------------------------


class TestBearerJtiValidateRaises:
    """Bearer path leaves user_jti unset when validate_token raises."""

    def test_ac3_invalid_token_leaves_user_jti_unset_no_exception(self):
        """AC3a: InvalidTokenError (non-JWT / malformed credential) must not
        propagate and must not set user_jti."""
        result, request, _ = _run_bearer_auth(
            validate_token_raises=InvalidTokenError("not a JWT")
        )

        assert result.username == _USERNAME
        assert not hasattr(request.state, "user_jti")

    def test_ac3_expired_token_leaves_user_jti_unset_no_exception(self):
        """AC3b: TokenExpiredError must not propagate and must not set user_jti."""
        result, request, _ = _run_bearer_auth(
            validate_token_raises=TokenExpiredError("token expired")
        )

        assert result.username == _USERNAME
        assert not hasattr(request.state, "user_jti")


# ---------------------------------------------------------------------------
# AC4: Session cookie path unchanged
# ---------------------------------------------------------------------------


class TestSessionCookieJtiUnchanged:
    """Session cookie path must continue to set user_jti from the cookie value."""

    def test_ac4_session_cookie_sets_user_jti_from_cookie(self):
        """AC4: user_jti is set to the session cookie value when cookie auth succeeds."""
        from code_indexer.server.web.auth import SessionData

        session_cookie_value = "session-cookie-fixture"
        mock_request = MagicMock()
        mock_request.cookies = {"session": session_cookie_value}
        mock_request.state = types.SimpleNamespace()

        session_data = SessionData(
            username=_USERNAME,
            role="admin",
            csrf_token="csrf-fixture",
            created_at=datetime.now(timezone.utc).timestamp(),
        )
        user = _make_user()

        mock_user_manager = MagicMock()
        mock_user_manager.get_user.return_value = user

        with patch("code_indexer.server.web.auth.get_session_manager") as mock_gsm:
            mock_session_manager = MagicMock()
            mock_session_manager.get_session.return_value = session_data
            mock_gsm.return_value = mock_session_manager

            with patch(
                "code_indexer.server.auth.dependencies.user_manager",
                mock_user_manager,
            ):
                result = _hybrid_auth_impl(
                    request=mock_request,
                    credentials=None,
                    require_admin=False,
                )

        assert result is user
        assert mock_request.state.user_jti == session_cookie_value
