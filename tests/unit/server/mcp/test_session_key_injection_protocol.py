"""Tests for session_key injection through _invoke_handler and handle_tools_call.

Bug: _invoke_handler never passes session_key to handlers, breaking TOTP elevation.

AC1: _invoke_handler with a handler that has session_key in its sig receives
     session_key=session_id when session_id is provided.

AC2: _invoke_handler with a handler that has session_key in its sig receives
     session_key="" when session_id is None.

AC3: _invoke_handler with an elevation-decorated handler (has __wrapped__ +
     **kwargs) receives session_key=session_id when session_id is provided.
     The inner_handler does NOT declare session_key — session_key NOT in
     inspect.signature(decorated).parameters — so only the wrapper **kwargs
     path can inject it.

AC4: _invoke_handler with a plain handler (no session_key, no __wrapped__)
     does NOT receive session_key (no TypeError).

AC5: _invoke_handler with a session_state-accepting handler still receives
     session_state alongside session_key injection.

AC6: handle_tools_call passes session_id as session_key to elevate_session handler.

AC7: handle_tools_call passes session_id as session_key to an
     elevation-decorated handler whose inner implementation does NOT declare
     session_key — verifying the wrapper **kwargs path is exercised.
"""

import contextlib
import inspect
from datetime import datetime
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock, patch

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
    The inner_handler does NOT declare session_key — the wrapper receives it via
    **kwargs and the real decorator's kill-switch passthrough delivers it.

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
) -> Any:
    """Invoke handle_tools_call for a tool with all deps patched.

    Shared by AC6 and AC7 to eliminate invocation boilerplate.
    """
    async with _handle_tools_call_context(tool_name, handler_fn) as (
        user_with_perms,
        handle_tools_call,
    ):
        return await handle_tools_call(
            params={"name": tool_name, "arguments": {}},
            user=user_with_perms,
            session_id=session_id,
        )


# ---------------------------------------------------------------------------
# AC1: handler with explicit session_key param gets session_key=session_id
# ---------------------------------------------------------------------------


async def test_ac1_handler_with_session_key_param_receives_session_id():
    """Handler declaring session_key: str gets the session_id injected."""
    result, received = await _invoke_handler_with_session_key_handler(
        session_id=_SESSION_ID
    )

    assert result == {"ok": True}
    assert received["session_key"] == _SESSION_ID, (
        f"Expected session_key={_SESSION_ID!r}, got {received['session_key']!r}. "
        "session_id is not being injected into handlers with session_key param."
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
    reading __mcp_requires_session_key__ marker), not gate logic.
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
        )

    assert result == {"elevated": True}
    assert received["session_key"] == _SESSION_ID, (
        f"Expected session_key={_SESSION_ID!r} via __mcp_requires_session_key__ path, "
        f"got {received['session_key']!r}."
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
    )

    assert result == {"both": True}
    assert received["session_state"] is session_state_val, (
        "session_state must still be injected alongside session_key"
    )
    assert received["session_key"] == _SESSION_ID, (
        f"Expected session_key={_SESSION_ID!r}, got {received['session_key']!r}"
    )


# ---------------------------------------------------------------------------
# AC6: handle_tools_call passes session_id as session_key to elevate_session
# ---------------------------------------------------------------------------


async def test_ac6_handle_tools_call_passes_session_id_to_elevate_session():
    """handle_tools_call injects session_id as session_key into elevate_session."""
    received: dict = {}

    def fake_elevate_session(args: Dict, user: User, session_key: str = "") -> Dict:
        received["session_key"] = session_key
        return {"elevated": True, "scope": "full"}

    await _call_handle_tools_call("elevate_session", fake_elevate_session)

    assert received.get("session_key") == _SESSION_ID, (
        f"Expected session_key={_SESSION_ID!r} in elevate_session, "
        f"got {received.get('session_key')!r}. "
        "handle_tools_call does not pass session_id as session_key."
    )


# ---------------------------------------------------------------------------
# AC7: handle_tools_call passes session_id to elevation-decorated handler
#
# inner_handler has NO session_key param — only the wrapper **kwargs path
# can deliver session_key. Verifies the real elevation decorator path.
# ---------------------------------------------------------------------------


async def test_ac7_handle_tools_call_passes_session_id_to_elevation_decorated_handler():
    """handle_tools_call injects session_id via **kwargs to elevation-decorated handler."""
    decorated_handler, received = _make_decorated_handler()

    await _call_handle_tools_call("some_gated_tool", decorated_handler)

    assert received.get("session_key") == _SESSION_ID, (
        f"Expected session_key={_SESSION_ID!r} via **kwargs path, "
        f"got {received.get('session_key')!r}. "
        "handle_tools_call does not pass session_id via **kwargs to decorated handlers."
    )
