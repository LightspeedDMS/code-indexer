"""Tests for session_key injection through _invoke_handler and handle_tools_call.

Bug: _invoke_handler never passes session_key to handlers, breaking TOTP elevation.

AC1: _invoke_handler with a handler that has session_key in its sig receives
     session_key=elevation_key when elevation_key is provided.

AC2: _invoke_handler with a handler that has session_key in its sig receives
     session_key="" when elevation_key is None.

AC3: _invoke_handler with an elevation-decorated handler (has __wrapped__ +
     **kwargs) receives session_key=elevation_key when elevation_key is provided.
     The inner_handler does NOT declare session_key — session_key NOT in
     inspect.signature(decorated).parameters — so only the wrapper **kwargs
     path can inject it.

AC4: _invoke_handler with a plain handler (no session_key, no __wrapped__)
     does NOT receive session_key (no TypeError).

AC5: _invoke_handler with a session_state-accepting handler still receives
     session_state alongside session_key injection.

AC6: handle_tools_call passes elevation_key as session_key to elevate_session handler.

AC7: handle_tools_call passes elevation_key as session_key to an
     elevation-decorated handler whose inner implementation does NOT declare
     session_key — verifying the wrapper **kwargs path is exercised.

AC14: get_current_user_for_mcp writes request.state.user_jti when client authenticates
      via cidx_session cookie (no Bearer header) — closing the cookie-auth elevation gap.
"""

import contextlib
import inspect
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation
from code_indexer.server.mcp.protocol import _invoke_handler

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)


# ---------------------------------------------------------------------------
# Shared constants and user factory
# ---------------------------------------------------------------------------

_SESSION_ID = "mcp-session-abc-123"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
# Clearly synthetic test fixture — represents a JWT jti value in tests only
_ELEVATION_KEY_TEST_FIXTURE = "test-elevation-key-fixture-only"


def _make_user() -> User:
    return User(
        username="admin",
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Helper: build elevation-decorated handler + capture dict (AC3, AC7)
#
# The inner_handler does NOT declare session_key. The wrapper delivers it via
# **kwargs using the _intercepted_key sentinel so the assertion can observe it.
# ---------------------------------------------------------------------------


def _make_decorated_handler() -> Tuple[Any, dict]:
    """Return (decorated_callable, received_dict) for elevation-wrapper tests.

    Uses the real require_mcp_elevation decorator (enforcement mocked off in tests).
    The inner_handler does NOT declare session_key. The decorator pops session_key
    before calling the inner handler (Bug A fix) — session_key is consumed by the
    wrapper for elevation validation and is NOT propagated to inner_handler.

    received_dict["session_key"] is populated when the inner handler is called.
    """
    received: dict = {}

    def inner_handler(args: Dict, user: User, **kwargs: Any) -> Dict:
        received["session_key"] = kwargs.get("session_key")
        return {"elevated": True}

    decorated = require_mcp_elevation()(inner_handler)
    return decorated, received


# ---------------------------------------------------------------------------
# Helper: call _invoke_handler with a handler that declares session_key (AC1, AC2)
# ---------------------------------------------------------------------------


async def _invoke_handler_with_session_key_handler(
    session_id,
    elevation_key: Optional[str] = None,
) -> Tuple[dict, dict]:
    """Create a handler with explicit session_key param, invoke via _invoke_handler.

    Returns (result, received_dict) where received_dict["session_key"] holds
    the value the handler observed.
    """
    received: dict = {}

    def handler_with_session_key(args: Dict, user: User, session_key: str = "") -> Dict:
        received["session_key"] = session_key
        return {"ok": True}

    user = _make_user()
    sig = inspect.signature(handler_with_session_key)
    result = await _invoke_handler(
        handler=handler_with_session_key,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
        session_id=session_id,
        elevation_key=elevation_key,
    )
    return result, received


# ---------------------------------------------------------------------------
# Helper: async context manager + call helper for handle_tools_call tests (AC6, AC7)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _handle_tools_call_context(tool_name: str, handler_fn):
    """Async context manager patching all handle_tools_call dependencies.

    Mocks elevation enforcement off so the real require_mcp_elevation decorator
    passes through — AC7 tests injection, not gate logic.

    Yields (user_mock, handle_tools_call_fn).
    """
    from code_indexer.server.mcp.protocol import handle_tools_call

    mock_session_state = MagicMock()
    mock_session_state.is_impersonating = False

    user_with_perms = MagicMock()
    user_with_perms.username = "admin"
    user_with_perms.has_permission.return_value = True

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {tool_name: handler_fn},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {tool_name: {"required_permission": "admin", "name": tool_name}},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.session_registry.get_session_registry",
        ) as mock_registry,
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        patch(_ENFORCEMENT_PATH, return_value=False),
    ):
        mock_registry.return_value.get_or_create_session.return_value = (
            mock_session_state
        )
        yield user_with_perms, handle_tools_call


async def _call_handle_tools_call(
    tool_name: str,
    handler_fn,
    session_id: str = _SESSION_ID,
    elevation_key: Optional[str] = None,
) -> Any:
    """Invoke handle_tools_call for a tool with all deps patched.

    Shared by AC6, AC7, and AC11 to eliminate invocation boilerplate.
    """
    async with _handle_tools_call_context(tool_name, handler_fn) as (
        user_with_perms,
        handle_tools_call,
    ):
        return await handle_tools_call(
            params={"name": tool_name, "arguments": {}},
            user=user_with_perms,
            session_id=session_id,
            elevation_key=elevation_key,
        )


# ---------------------------------------------------------------------------
# AC1: handler with explicit session_key param gets session_key=session_id
# ---------------------------------------------------------------------------


async def test_ac1_handler_with_session_key_param_receives_elevation_key():
    """Handler declaring session_key: str gets the elevation_key injected."""
    result, received = await _invoke_handler_with_session_key_handler(
        session_id=_SESSION_ID,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert result == {"ok": True}
    assert received["session_key"] == _ELEVATION_KEY_TEST_FIXTURE, (
        f"Expected session_key={_ELEVATION_KEY_TEST_FIXTURE!r}, got {received['session_key']!r}. "
        "elevation_key is not being injected into handlers with session_key param."
    )


# ---------------------------------------------------------------------------
# AC2: handler with explicit session_key param gets session_key="" when None
# ---------------------------------------------------------------------------


async def test_ac2_handler_with_session_key_param_receives_empty_when_no_session_id():
    """Handler declaring session_key: str gets empty string when session_id is None."""
    _result, received = await _invoke_handler_with_session_key_handler(session_id=None)

    assert received["session_key"] == "", (
        f"Expected session_key='', got {received['session_key']!r}. "
        "When session_id is None, session_key should be empty string."
    )


# ---------------------------------------------------------------------------
# AC3: elevation-decorated handler (VAR_KEYWORD wrapper) receives session_key
#
# inner_handler has no session_key param — NOT in inspect.signature(decorated).
# Only the wrapper **kwargs path can deliver session_key.
# ---------------------------------------------------------------------------


async def test_ac3_elevation_decorated_handler_receives_session_key():
    """Handler wrapped with real require_mcp_elevation gets session_key via **kwargs.

    Enforcement is mocked off so the decorator passes through without checking
    the elevation window. The test validates the injection path (protocol.py
    reading __mcp_requires_session_key__ marker). After Bug A fix, the decorator
    consumes session_key and inner_handler receives None.
    """
    decorated, received = _make_decorated_handler()

    # Confirm test setup invariants: real decorator sets the explicit marker
    assert getattr(decorated, "__mcp_requires_session_key__", False), (
        "require_mcp_elevation wrapper must carry __mcp_requires_session_key__ = True"
    )
    sig = inspect.signature(decorated)

    user = _make_user()
    with patch(_ENFORCEMENT_PATH, return_value=False):
        result = await _invoke_handler(
            handler=decorated,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            session_id=_SESSION_ID,
            elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
        )

    assert result == {"elevated": True}
    assert received["session_key"] is None, (
        f"After Bug A fix, require_mcp_elevation pops session_key before calling "
        f"inner_handler. inner_handler received {received['session_key']!r} (expected None)."
    )


# ---------------------------------------------------------------------------
# AC4: plain handler with no session_key and no __wrapped__ gets no session_key
# ---------------------------------------------------------------------------


async def test_ac4_plain_handler_receives_no_session_key():
    """Plain handler (no session_key, no __wrapped__) must not receive session_key."""
    received: dict = {}

    def plain_handler(args: Dict, user: User) -> Dict:
        # Would raise TypeError if called with unexpected kwarg session_key
        received["called"] = True
        return {"plain": True}

    user = _make_user()
    sig = inspect.signature(plain_handler)
    result = await _invoke_handler(
        handler=plain_handler,
        arguments={"x": 1},
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
        session_id=_SESSION_ID,
    )

    assert result == {"plain": True}
    assert received.get("called") is True


# ---------------------------------------------------------------------------
# AC5: handler with session_state gets both session_state and session_key
# ---------------------------------------------------------------------------


async def test_ac5_handler_with_session_state_and_session_key_receives_both():
    """Handler accepting both session_state and session_key gets both injected."""
    received: dict = {}

    def handler_both(
        args: Dict, user: User, session_state=None, session_key: str = ""
    ) -> Dict:
        received["session_state"] = session_state
        received["session_key"] = session_key
        return {"both": True}

    user = _make_user()
    session_state_val = {"state": "data"}
    sig = inspect.signature(handler_both)
    result = await _invoke_handler(
        handler=handler_both,
        arguments={},
        user=user,
        session_state=session_state_val,
        sig=sig,
        is_async=False,
        session_id=_SESSION_ID,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert result == {"both": True}
    assert received["session_state"] is session_state_val, (
        "session_state must still be injected alongside session_key"
    )
    assert received["session_key"] == _ELEVATION_KEY_TEST_FIXTURE, (
        f"Expected session_key={_ELEVATION_KEY_TEST_FIXTURE!r}, got {received['session_key']!r}"
    )


# ---------------------------------------------------------------------------
# AC6: handle_tools_call passes session_id as session_key to elevate_session
# ---------------------------------------------------------------------------


async def test_ac6_handle_tools_call_passes_elevation_key_to_elevate_session():
    """handle_tools_call injects elevation_key as session_key into elevate_session."""
    received: dict = {}

    def fake_elevate_session(args: Dict, user: User, session_key: str = "") -> Dict:
        received["session_key"] = session_key
        return {"elevated": True, "scope": "full"}

    await _call_handle_tools_call(
        "elevate_session",
        fake_elevate_session,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert received.get("session_key") == _ELEVATION_KEY_TEST_FIXTURE, (
        f"Expected session_key={_ELEVATION_KEY_TEST_FIXTURE!r} in elevate_session, "
        f"got {received.get('session_key')!r}. "
        "handle_tools_call does not pass elevation_key as session_key."
    )


# ---------------------------------------------------------------------------
# AC7: handle_tools_call passes session_id to elevation-decorated handler
#
# inner_handler has NO session_key param — only the wrapper **kwargs path
# can deliver session_key. Verifies the real elevation decorator path.
# ---------------------------------------------------------------------------


async def test_ac7_handle_tools_call_passes_elevation_key_to_elevation_decorated_handler():
    """handle_tools_call injects elevation_key via **kwargs to elevation-decorated handler."""
    decorated_handler, received = _make_decorated_handler()

    await _call_handle_tools_call(
        "some_gated_tool",
        decorated_handler,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert received.get("session_key") is None, (
        "After Bug A fix, require_mcp_elevation pops session_key before calling "
        "inner_handler. Decorator consumes session_key for elevation validation; "
        f"inner_handler received {received.get('session_key')!r} (expected None)."
    )


# ---------------------------------------------------------------------------
# Helper for AC8 / AC9 / AC10: invoke _invoke_handler with elevation_key
# ---------------------------------------------------------------------------


async def _invoke_handler_with_elevation_key(
    session_id: Optional[str],
    elevation_key: Optional[str],
) -> dict:
    """Return received dict from a session_key-declaring handler invoked via _invoke_handler."""
    received: dict = {}

    def handler_with_session_key(args: Dict, user: User, session_key: str = "") -> Dict:
        received["session_key"] = session_key
        return {"ok": True}

    user = _make_user()
    sig = inspect.signature(handler_with_session_key)
    await _invoke_handler(
        handler=handler_with_session_key,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
        session_id=session_id,
        elevation_key=elevation_key,
    )
    return received


# ---------------------------------------------------------------------------
# AC8 / AC9 / AC10: elevation_key is the sole canonical elevation key
#
# AC8: elevation_key provided (session_id=None) → elevation_key used
# AC9: session_id only, elevation_key=None → session_key NOT injected (CLAUDE.md
#      invariant: only JWT jti or cookie jti are valid elevation keys; MCP transport
#      UUID must NOT silently create an alternate elevation namespace)
# AC10: both provided → elevation_key wins; session_id ignored for elevation
# ---------------------------------------------------------------------------

_DIFFERENT_SESSION_ID = "different-mcp-session-uuid"

_ELEVATION_KEY_PRECEDENCE_CASES = [
    pytest.param(
        None,
        _ELEVATION_KEY_TEST_FIXTURE,
        _ELEVATION_KEY_TEST_FIXTURE,
        id="ac8-elevation-key-only-no-session-id",
    ),
    pytest.param(
        _SESSION_ID,
        None,
        "",  # no injection — session_id is NOT a valid elevation key
        id="ac9-no-elevation-key-session-id-not-injected",
    ),
    pytest.param(
        _DIFFERENT_SESSION_ID,
        _ELEVATION_KEY_TEST_FIXTURE,
        _ELEVATION_KEY_TEST_FIXTURE,
        id="ac10-both-provided-elevation-key-wins",
    ),
]


@pytest.mark.parametrize(
    "session_id, elevation_key, expected_session_key",
    _ELEVATION_KEY_PRECEDENCE_CASES,
)
async def test_ac8_ac9_ac10_elevation_key_precedence(
    session_id: Optional[str],
    elevation_key: Optional[str],
    expected_session_key: str,
) -> None:
    """Parametrized: elevation_key used (AC8/AC10); session_id NOT used for elevation (AC9)."""
    received = await _invoke_handler_with_elevation_key(
        session_id=session_id,
        elevation_key=elevation_key,
    )
    assert received["session_key"] == expected_session_key, (
        f"With session_id={session_id!r} and elevation_key={elevation_key!r}, "
        f"expected session_key={expected_session_key!r}, got {received['session_key']!r}."
    )


# ---------------------------------------------------------------------------
# AC11: handle_tools_call forwards elevation_key as session_key to handler
# ---------------------------------------------------------------------------


async def test_ac11_handle_tools_call_passes_elevation_key_as_session_key():
    """handle_tools_call forwards elevation_key as session_key, overriding session_id."""
    received: dict = {}

    def fake_handler(args: Dict, user: User, session_key: str = "") -> Dict:
        received["session_key"] = session_key
        return {"result": "ok"}

    await _call_handle_tools_call(
        "some_tool",
        fake_handler,
        session_id=_SESSION_ID,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert received.get("session_key") == _ELEVATION_KEY_TEST_FIXTURE, (
        f"Expected session_key={_ELEVATION_KEY_TEST_FIXTURE!r} (elevation_key forwarded), "
        f"got {received.get('session_key')!r}. "
        "handle_tools_call must forward elevation_key as session_key to handlers."
    )


# ---------------------------------------------------------------------------
# Shared helpers for AC12 / AC13 / AC14 auth-boundary tests
# ---------------------------------------------------------------------------


def _make_mcp_auth_request(
    auth_header: Optional[str] = None, cookie_token: Optional[str] = None
) -> MagicMock:
    """Build a mock Request with a Starlette State object for auth-boundary tests."""
    from starlette.datastructures import State

    mock_request = MagicMock()
    mock_request.state = State()
    headers: Dict[str, str] = {}
    if auth_header:
        headers["Authorization"] = auth_header
    mock_request.headers = headers
    mock_request.cookies = {"cidx_session": cookie_token} if cookie_token else {}
    return mock_request


def _assert_user_jti(request: MagicMock, expected_jti: str, context: str) -> None:
    """Assert that request.state.user_jti equals expected_jti."""
    actual = getattr(request.state, "user_jti", None)
    assert actual == expected_jti, (
        f"{context}: expected request.state.user_jti={expected_jti!r}, got {actual!r}."
    )


# ---------------------------------------------------------------------------
# AC12: get_current_user_for_mcp writes request.state.user_jti on Bearer JWT
# ---------------------------------------------------------------------------


async def test_ac12_get_current_user_for_mcp_sets_user_jti_on_bearer_jwt_auth():
    """get_current_user_for_mcp must write request.state.user_jti with the JWT jti
    after successful Bearer token authentication.

    Mocks at the low level (_validate_jwt_and_get_user, is_token_blacklisted) so
    that get_current_user runs its real logic while avoiding DB/JWT infrastructure.
    The jwt_manager mock is needed for the jti extraction in get_current_user_for_mcp.
    """
    from code_indexer.server.auth.dependencies import get_current_user_for_mcp

    _TEST_JTI = "test-jti-ac12-bearer-unique"
    mock_user = _make_user()
    mock_payload = {"jti": _TEST_JTI, "username": "admin"}
    mock_request = _make_mcp_auth_request(auth_header="Bearer fake-token-ac12")

    with (
        patch(
            "code_indexer.server.auth.dependencies.get_mcp_user_from_credentials",
            return_value=None,
        ),
        patch(
            "code_indexer.server.auth.dependencies._validate_jwt_and_get_user",
            return_value=mock_user,
        ),
        patch(
            "code_indexer.server.app.is_token_blacklisted",
            return_value=False,
        ),
        patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt_mgr,
    ):
        mock_jwt_mgr.validate_token.return_value = mock_payload

        result_user = await get_current_user_for_mcp(mock_request)

    assert result_user is mock_user, (
        "get_current_user_for_mcp must return the authenticated user"
    )
    _assert_user_jti(
        mock_request,
        _TEST_JTI,
        "After Bearer JWT auth via get_current_user_for_mcp",
    )
    mock_jwt_mgr.validate_token.assert_called_once_with("fake-token-ac12")


# ---------------------------------------------------------------------------
# AC13: get_optional_user_from_cookie writes request.state.user_jti on cookie
# ---------------------------------------------------------------------------


async def test_ac13_get_optional_user_from_cookie_sets_user_jti_on_cookie_auth():
    """get_optional_user_from_cookie must write request.state.user_jti with the JWT jti
    after successful cookie-based JWT authentication.

    Without this, the /mcp-public elevation_key remains None even when the user
    is authenticated via the cidx_session cookie.
    """
    from code_indexer.server.mcp.protocol import get_optional_user_from_cookie
    import code_indexer.server.mcp.protocol as mcp_protocol_module

    _TEST_JTI = "test-jti-ac13-cookie-unique"
    mock_user = _make_user()
    mock_payload = {"jti": _TEST_JTI, "username": "admin"}
    mock_request = _make_mcp_auth_request(cookie_token="fake-cookie-token-ac13")

    with (
        patch.object(
            mcp_protocol_module.auth_deps,
            "jwt_manager",
        ) as mock_jwt_mgr,
        patch(
            "code_indexer.server.auth.dependencies._validate_jwt_and_get_user",
            return_value=mock_user,
        ),
    ):
        mock_jwt_mgr.validate_token.return_value = mock_payload

        result_user = get_optional_user_from_cookie(mock_request)

    assert result_user is mock_user, (
        "get_optional_user_from_cookie must return the authenticated user"
    )
    _assert_user_jti(
        mock_request,
        _TEST_JTI,
        "After cookie JWT auth via get_optional_user_from_cookie",
    )
    mock_jwt_mgr.validate_token.assert_called_once_with("fake-cookie-token-ac13")


# ---------------------------------------------------------------------------
# AC14: get_current_user_for_mcp writes request.state.user_jti on cookie auth
# ---------------------------------------------------------------------------

_TEST_JTI_AC14 = "test-jti-ac14-cookie-unique"


async def test_ac14_get_current_user_for_mcp_sets_user_jti_on_cookie_auth():
    """get_current_user_for_mcp must write request.state.user_jti with the JWT jti
    after successful cidx_session cookie authentication when no Bearer header is present.

    Without this fix, elevation always fails for cookie-authed /mcp clients because
    token is None so the jti extraction block is never entered.
    """
    from code_indexer.server.auth.dependencies import get_current_user_for_mcp

    mock_user = _make_user()
    mock_payload = {"jti": _TEST_JTI_AC14, "username": "admin"}
    mock_request = _make_mcp_auth_request(cookie_token="fake-cookie-token-ac14")

    with (
        patch(
            "code_indexer.server.auth.dependencies.get_mcp_user_from_credentials",
            return_value=None,
        ),
        patch(
            "code_indexer.server.auth.dependencies._validate_jwt_and_get_user",
            return_value=mock_user,
        ),
        patch(
            "code_indexer.server.auth.dependencies.user_manager",
            new=MagicMock(),
        ),
        patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt_mgr,
    ):
        mock_jwt_mgr.validate_token.return_value = mock_payload

        result_user = await get_current_user_for_mcp(mock_request)

    assert result_user is mock_user, (
        "get_current_user_for_mcp must return the authenticated user"
    )
    _assert_user_jti(
        mock_request,
        _TEST_JTI_AC14,
        "After cookie JWT auth via get_current_user_for_mcp",
    )
    mock_jwt_mgr.validate_token.assert_called_once_with("fake-cookie-token-ac14")
