"""Story #1491 AC6: async-handler dispatch branch gains a deadline.

Finding B6: protocol.py's async branch (`await handler(...)`) has no
asyncio.wait_for guard, unlike the sync branch (executor + wait_for).
This test proves the async branch is now bounded by timeout_seconds and
produces the SAME timeout error shape the sync branch already produces,
while a normally-completing async handler is unaffected.

Note: this project's pyproject.toml sets asyncio_mode = "auto", so bare
`async def test_...` functions are collected and awaited automatically --
no @pytest.mark.asyncio decorator needed. This mirrors the sibling file
test_invoke_handler_executor.py exactly.
"""

import asyncio
import inspect
from datetime import datetime

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler


def _make_user() -> User:
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


async def test_async_handler_exceeding_deadline_times_out():
    """An async handler that never completes within timeout_seconds must
    produce the same timeout error shape the sync branch already returns."""

    async def slow_async_handler(arguments, user):
        await asyncio.sleep(10)
        return {"should": "never reach here"}

    user = _make_user()
    sig = inspect.signature(slow_async_handler)
    result = await _invoke_handler(
        handler=slow_async_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
        timeout_seconds=0.05,
    )

    assert result["success"] is False
    assert "timed out" in result["error"]


async def test_async_handler_completing_normally_is_unaffected():
    """A normally-completing async handler must return its real result
    unchanged when a deadline is present but never exceeded."""

    async def fast_async_handler(arguments, user):
        return {"async": True}

    user = _make_user()
    sig = inspect.signature(fast_async_handler)
    result = await _invoke_handler(
        handler=fast_async_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
        timeout_seconds=30,
    )

    assert result == {"async": True}
