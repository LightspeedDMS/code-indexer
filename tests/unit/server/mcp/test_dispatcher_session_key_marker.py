"""Tests for __mcp_requires_session_key__ marker on consolidated MCP dispatcher functions.

Bug: Epic #985 consolidated individual MCP tools into action-param dispatcher functions.
The public dispatchers are UNDECORATED, so they lack __mcp_requires_session_key__ = True.
Protocol.py (lines 207-210) checks this marker to inject session_key into handler kwargs.
Without the marker, session_key is never added to extra_kwargs, and inner handlers'
@require_mcp_elevation() fails with "No session key".

AC1: handle_manage_group_members.__mcp_requires_session_key__ is True
AC2: handle_manage_group_repos.__mcp_requires_session_key__ is True
AC3: handle_list_mcp_credentials.__mcp_requires_session_key__ is True
AC4: handle_manage_mcp_credential.__mcp_requires_session_key__ is True
AC5: protocol.py dispatch flow injects session_key into the dispatcher's **kwargs
     when the dispatcher carries __mcp_requires_session_key__ = True.
"""

import inspect
from typing import Any, Dict

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.admin import (
    handle_manage_group_members,
    handle_manage_group_repos,
)
from code_indexer.server.mcp.handlers.admin.mcp_credentials import (
    handle_list_mcp_credentials,
    handle_manage_mcp_credential,
)
from code_indexer.server.mcp.protocol import _invoke_handler

from datetime import datetime

_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
# Clearly synthetic test fixture — represents a JWT jti value in tests only
_ELEVATION_KEY_TEST_FIXTURE = "test-elevation-key-dispatcher-fixture"
_SESSION_ID = "mcp-session-dispatcher-test-123"


def _make_admin_user() -> User:
    return User(
        username="admin",
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# AC1: handle_manage_group_members has __mcp_requires_session_key__ = True
# ---------------------------------------------------------------------------


def test_handle_manage_group_members_has_session_key_marker():
    """handle_manage_group_members must carry __mcp_requires_session_key__ = True.

    Without this marker, protocol.py's _invoke_handler never injects session_key
    into the dispatcher's **kwargs, so inner elevation-gated handlers fail.
    """
    assert (
        getattr(handle_manage_group_members, "__mcp_requires_session_key__", False)
        is True
    ), (
        "handle_manage_group_members is missing __mcp_requires_session_key__ = True. "
        "Add it after the function definition: "
        "handle_manage_group_members.__mcp_requires_session_key__ = True"
    )


# ---------------------------------------------------------------------------
# AC2: handle_manage_group_repos has __mcp_requires_session_key__ = True
# ---------------------------------------------------------------------------


def test_handle_manage_group_repos_has_session_key_marker():
    """handle_manage_group_repos must carry __mcp_requires_session_key__ = True.

    Without this marker, protocol.py's _invoke_handler never injects session_key
    into the dispatcher's **kwargs, so inner elevation-gated handlers fail.
    """
    assert (
        getattr(handle_manage_group_repos, "__mcp_requires_session_key__", False)
        is True
    ), (
        "handle_manage_group_repos is missing __mcp_requires_session_key__ = True. "
        "Add it after the function definition: "
        "handle_manage_group_repos.__mcp_requires_session_key__ = True"
    )


# ---------------------------------------------------------------------------
# AC3: handle_list_mcp_credentials has __mcp_requires_session_key__ = True
# ---------------------------------------------------------------------------


def test_handle_list_mcp_credentials_has_session_key_marker():
    """handle_list_mcp_credentials must carry __mcp_requires_session_key__ = True.

    Without this marker, protocol.py's _invoke_handler never injects session_key
    into the dispatcher's **kwargs, so inner elevation-gated handlers fail.
    """
    assert (
        getattr(handle_list_mcp_credentials, "__mcp_requires_session_key__", False)
        is True
    ), (
        "handle_list_mcp_credentials is missing __mcp_requires_session_key__ = True. "
        "Add it after the function definition: "
        "handle_list_mcp_credentials.__mcp_requires_session_key__ = True"
    )


# ---------------------------------------------------------------------------
# AC4: handle_manage_mcp_credential has __mcp_requires_session_key__ = True
# ---------------------------------------------------------------------------


def test_handle_manage_mcp_credential_has_session_key_marker():
    """handle_manage_mcp_credential must carry __mcp_requires_session_key__ = True.

    Without this marker, protocol.py's _invoke_handler never injects session_key
    into the dispatcher's **kwargs, so inner elevation-gated handlers fail.
    """
    assert (
        getattr(handle_manage_mcp_credential, "__mcp_requires_session_key__", False)
        is True
    ), (
        "handle_manage_mcp_credential is missing __mcp_requires_session_key__ = True. "
        "Add it after the function definition: "
        "handle_manage_mcp_credential.__mcp_requires_session_key__ = True"
    )


# ---------------------------------------------------------------------------
# AC5: protocol.py _invoke_handler injects session_key via the marker
#
# Simulates the protocol.py dispatch flow: create a dispatcher that captures
# the session_key it receives, mark it with __mcp_requires_session_key__ = True,
# then call _invoke_handler and verify session_key arrives in **kwargs.
# ---------------------------------------------------------------------------


async def test_protocol_injects_session_key_via_dispatcher_marker():
    """_invoke_handler injects session_key into dispatcher **kwargs when marker is set.

    This test simulates the full protocol.py Case B injection path:
      - dispatcher carries __mcp_requires_session_key__ = True (no session_key param)
      - _invoke_handler reads the marker via getattr(handler, '__mcp_requires_session_key__', False)
      - session_key is added to extra_kwargs and passed to the dispatcher via **extra_kwargs
      - The dispatcher's **kwargs captures it

    This is the actual bug scenario: without the marker, session_key is never injected.
    """
    received: dict = {}

    def fake_dispatcher(
        args: Dict[str, Any], user: User, **kwargs: Any
    ) -> Dict[str, Any]:
        """Mimics a consolidated dispatcher: accepts **kwargs, captures session_key."""
        received["session_key"] = kwargs.get("session_key")
        return {"dispatched": True}

    # Apply the marker (this is what the fix does for the real dispatchers)
    fake_dispatcher.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]

    user = _make_admin_user()
    sig = inspect.signature(fake_dispatcher)

    result = await _invoke_handler(
        handler=fake_dispatcher,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
        session_id=_SESSION_ID,
        elevation_key=_ELEVATION_KEY_TEST_FIXTURE,
    )

    assert result == {"dispatched": True}, (
        f"Dispatcher should return its result, got {result!r}"
    )
    assert received.get("session_key") == _ELEVATION_KEY_TEST_FIXTURE, (
        f"Expected dispatcher **kwargs to contain session_key={_ELEVATION_KEY_TEST_FIXTURE!r}, "
        f"got {received.get('session_key')!r}. "
        "protocol.py Case B injection is broken or marker is not being read."
    )
