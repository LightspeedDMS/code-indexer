"""Unit tests for asyncio.wait_for timeout wrapping in _invoke_handler.

Bug #1008: git_blame MCP tool times out on repos with deep history.

Fix 3: Wrap run_in_executor with asyncio.wait_for(timeout=60.0) so that
sync handlers that hang do not block the event loop indefinitely.
"""

import asyncio
import inspect
from datetime import datetime
from unittest.mock import patch, AsyncMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler

HANDLER_TIMEOUT_SECONDS = 60


def _make_user() -> User:
    """Create a minimal User for testing."""
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


class TestProtocolTimeout:
    """Tests for asyncio.wait_for timeout wrapping in _invoke_handler."""

    @pytest.mark.asyncio
    async def test_slow_sync_handler_returns_timeout_error(self):
        """Slow sync handlers must return a timeout error dict, not block indefinitely.

        When asyncio.wait_for raises asyncio.TimeoutError, _invoke_handler must
        catch it and return the exact timeout error response.
        """

        def slow_handler(arguments, user):
            # In production this would block for minutes on deep repos
            return {"ok": True}

        user = _make_user()
        sig = inspect.signature(slow_handler)

        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
            mock_wait.side_effect = asyncio.TimeoutError()

            result = await _invoke_handler(
                handler=slow_handler,
                arguments={},
                user=user,
                session_state=None,
                sig=sig,
                is_async=False,
            )

        assert result == {
            "success": False,
            "error": f"Tool execution timed out after {HANDLER_TIMEOUT_SECONDS} seconds",
        }

    @pytest.mark.asyncio
    async def test_fast_sync_handler_completes_normally(self):
        """Fast sync handlers must complete and return their result normally.

        Regression test: the wait_for wrapper must not break the normal execution path.
        """

        def fast_handler(arguments, user):
            return {"ok": True, "value": 42}

        user = _make_user()
        sig = inspect.signature(fast_handler)

        result = await _invoke_handler(
            handler=fast_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
        )

        assert result == {"ok": True, "value": 42}
