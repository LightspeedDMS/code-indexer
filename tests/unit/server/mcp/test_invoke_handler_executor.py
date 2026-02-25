"""Unit tests for Story #276: _invoke_handler dispatches sync handlers via run_in_executor.

TDD cycle: tests written BEFORE the production code change.
All sync-handler tests must FAIL until protocol.py is updated.

Verifies:
- Sync handlers run on a thread-pool thread (NOT the event loop thread)
- Async handlers are awaited directly (same thread as event loop)
- Return values propagate correctly through the executor
- Exceptions propagate correctly through the executor
- Correct arguments forwarded for both session_state and no-session_state paths
"""

import asyncio
import inspect
import threading
from datetime import datetime

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler


def _make_user() -> User:
    """Create a minimal User for testing."""
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Test 1: sync handler (no session_state) runs on a worker thread
# ---------------------------------------------------------------------------


async def test_sync_handler_dispatched_via_run_in_executor():
    """Sync handler must NOT run on the event loop thread.

    Before the fix the sync branch calls handler() directly, which blocks the
    event loop and keeps the call on the same thread.  After the fix it calls
    loop.run_in_executor(), which moves the call to the default ThreadPoolExecutor.
    """
    event_loop_thread_id = threading.current_thread().ident
    handler_thread_id_container: list = []

    def sync_handler(arguments, user):
        handler_thread_id_container.append(threading.current_thread().ident)
        return {"ok": True}

    user = _make_user()
    sig = inspect.signature(sync_handler)
    result = await _invoke_handler(
        handler=sync_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
    )

    assert result == {"ok": True}
    assert len(handler_thread_id_container) == 1, "handler must have been called exactly once"
    handler_thread_id = handler_thread_id_container[0]
    assert handler_thread_id != event_loop_thread_id, (
        "Sync handler must run on a worker thread, not the event loop thread. "
        "The sync branch still blocks the event loop — run_in_executor not applied."
    )


# ---------------------------------------------------------------------------
# Test 2: sync handler with session_state receives all 3 arguments
# ---------------------------------------------------------------------------


async def test_sync_handler_with_session_state_uses_partial():
    """Sync handler with session_state parameter receives all three arguments."""
    received: dict = {}

    def sync_handler_with_state(arguments, user, session_state=None):
        received["arguments"] = arguments
        received["user"] = user
        received["session_state"] = session_state
        return "done"

    user = _make_user()
    session_state = {"key": "value"}
    sig = inspect.signature(sync_handler_with_state)
    result = await _invoke_handler(
        handler=sync_handler_with_state,
        arguments={"arg1": 1},
        user=user,
        session_state=session_state,
        sig=sig,
        is_async=False,
    )

    assert result == "done"
    assert received["arguments"] == {"arg1": 1}
    assert received["user"] is user
    assert received["session_state"] is session_state


# ---------------------------------------------------------------------------
# Test 3: sync handler without session_state receives exactly 2 arguments
# ---------------------------------------------------------------------------


async def test_sync_handler_without_session_state_uses_partial():
    """Sync handler without session_state parameter receives exactly two arguments."""
    received: dict = {}

    def sync_handler_no_state(arguments, user):
        received["arguments"] = arguments
        received["user"] = user
        return "result"

    user = _make_user()
    sig = inspect.signature(sync_handler_no_state)
    result = await _invoke_handler(
        handler=sync_handler_no_state,
        arguments={"x": 42},
        user=user,
        session_state={"should_not_appear": True},
        sig=sig,
        is_async=False,
    )

    assert result == "result"
    assert received["arguments"] == {"x": 42}
    assert received["user"] is user
    # session_state must NOT have been passed to this handler
    assert "session_state" not in received


# ---------------------------------------------------------------------------
# Test 4: async handler without session_state runs on the event loop thread
# ---------------------------------------------------------------------------


async def test_async_handler_awaited_directly():
    """Async handler is awaited directly and runs on the event loop thread."""
    event_loop_thread_id = threading.current_thread().ident
    handler_thread_id_container: list = []

    async def async_handler(arguments, user):
        handler_thread_id_container.append(threading.current_thread().ident)
        return {"async": True}

    user = _make_user()
    sig = inspect.signature(async_handler)
    result = await _invoke_handler(
        handler=async_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
    )

    assert result == {"async": True}
    assert len(handler_thread_id_container) == 1
    # Async handler must stay on the event loop thread
    assert handler_thread_id_container[0] == event_loop_thread_id, (
        "Async handler should run on the event loop thread (awaited directly)."
    )


# ---------------------------------------------------------------------------
# Test 5: async handler with session_state runs on the event loop thread
# ---------------------------------------------------------------------------


async def test_async_handler_with_session_state_awaited_directly():
    """Async handler with session_state is awaited directly, not via executor."""
    event_loop_thread_id = threading.current_thread().ident
    handler_thread_id_container: list = []

    async def async_handler_with_state(arguments, user, session_state=None):
        handler_thread_id_container.append(threading.current_thread().ident)
        return session_state

    user = _make_user()
    session_state = {"session": "data"}
    sig = inspect.signature(async_handler_with_state)
    result = await _invoke_handler(
        handler=async_handler_with_state,
        arguments={},
        user=user,
        session_state=session_state,
        sig=sig,
        is_async=True,
    )

    assert result is session_state
    assert len(handler_thread_id_container) == 1
    assert handler_thread_id_container[0] == event_loop_thread_id, (
        "Async handler should run on the event loop thread (awaited directly)."
    )


# ---------------------------------------------------------------------------
# Test 6: exception from sync handler propagates through the executor
# ---------------------------------------------------------------------------


async def test_sync_handler_exception_propagates_through_executor():
    """ValueError raised inside sync handler must propagate to the awaiting caller."""

    def failing_sync_handler(arguments, user):
        raise ValueError("test error")

    user = _make_user()
    sig = inspect.signature(failing_sync_handler)

    with pytest.raises(ValueError, match="test error"):
        await _invoke_handler(
            handler=failing_sync_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
        )


# ---------------------------------------------------------------------------
# Test 7: return value from sync handler propagates through the executor
# ---------------------------------------------------------------------------


async def test_sync_handler_return_value_propagates_through_executor():
    """Return value from sync handler must reach the caller unchanged."""

    def sync_handler_with_return(arguments, user):
        return {"result": "success", "echo": arguments}

    user = _make_user()
    arguments = {"query": "hello"}
    sig = inspect.signature(sync_handler_with_return)
    result = await _invoke_handler(
        handler=sync_handler_with_return,
        arguments=arguments,
        user=user,
        session_state=None,
        sig=sig,
        is_async=False,
    )

    assert result == {"result": "success", "echo": {"query": "hello"}}
