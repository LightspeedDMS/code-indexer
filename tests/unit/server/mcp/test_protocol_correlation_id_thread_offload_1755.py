"""Unit tests for Bug #1755: MCP sync-handler thread-offload loses correlation_id.

`_invoke_handler` in protocol.py dispatches sync handlers via
``loop.run_in_executor(None, bound)`` with no ``contextvars.copy_context()``
wrapping the offload. The correlation_id ContextVar (canonically stored in
``telemetry.correlation_bridge._correlation_id_var``, set by
``CorrelationBridgeMiddleware`` for every real request, and read by every MCP
handler via ``get_current_correlation_id()`` -- see
``mcp/handlers/admin/mcp_credentials.py``) is bound to the calling coroutine's
context and is therefore invisible inside the handler once execution crosses
into the executor thread pool.

This mirrors the identical thread-boundary trap already fixed correctly
elsewhere in this codebase:
- ``server/query/semantic_query_manager.py:1942``
  (``contextvars.copy_context()`` + ``executor.submit(ctx.run, fn)``)
- ``server/multi/multi_search_service.py:236``
  (``contextvars.copy_context()`` + ``executor.submit(ctx.run, ...)``)

Two independent occurrences of the missing ``copy_context()`` exist in
``_invoke_handler``'s sync branch: the exempt-tool early return (no outer
``wait_for``) and the default path (wrapped in ``asyncio.wait_for``). A
single parametrized test exercises both, since "get_file_content" is not in
``_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS`` (default/wait_for branch) while
"regex_search" is (exempt-tool early-return branch).
"""

import inspect
from datetime import datetime

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id,
    set_current_correlation_id,
)


def _make_user() -> User:
    """Create a minimal User for testing."""
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


class TestSyncHandlerThreadOffloadPreservesCorrelationId:
    """Bug #1755: sync handlers dispatched via run_in_executor must see the
    calling coroutine's correlation_id ContextVar inside the worker thread.

    Parametrized over both sync-dispatch branches in _invoke_handler, each of
    which contains its own independent (pre-fix) missing copy_context() call:
    - "get_file_content": not exempt -> asyncio.wait_for(run_in_executor(...))
    - "regex_search": exempt -> bare run_in_executor(...) early return
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name",
        ["get_file_content", "regex_search"],
    )
    async def test_sync_dispatch_preserves_correlation_id(self, tool_name):
        observed = {}

        def handler(arguments, user):
            observed["correlation_id"] = get_current_correlation_id()
            return {"ok": True}

        expected_correlation_id = f"test-correlation-id-{tool_name}"
        set_current_correlation_id(expected_correlation_id)
        try:
            user = _make_user()
            sig = inspect.signature(handler)

            result = await _invoke_handler(
                handler=handler,
                arguments={},
                user=user,
                session_state=None,
                sig=sig,
                is_async=False,
                tool_name=tool_name,
            )
        finally:
            set_current_correlation_id(None)  # type: ignore[arg-type]

        assert result == {"ok": True}
        assert observed["correlation_id"] == expected_correlation_id, (
            f"correlation_id was lost crossing the run_in_executor thread "
            f"boundary for tool_name={tool_name!r}: observed "
            f"{observed['correlation_id']!r}, expected "
            f"{expected_correlation_id!r}"
        )


class TestAsyncHandlerDispatchControlAlreadyPreservesCorrelationId:
    """Control: async handlers are awaited directly in the calling task's own
    context (no thread offload) -- the same shape as the REST code path,
    which never crosses a thread boundary. This must pass both before and
    after the fix, proving the fix touches only the sync/executor branches.
    """

    @pytest.mark.asyncio
    async def test_async_handler_dispatch_already_preserves_correlation_id(self):
        observed = {}

        async def handler(arguments, user):
            observed["correlation_id"] = get_current_correlation_id()
            return {"ok": True}

        expected_correlation_id = "test-correlation-id-async-control"
        set_current_correlation_id(expected_correlation_id)
        try:
            user = _make_user()
            sig = inspect.signature(handler)

            result = await _invoke_handler(
                handler=handler,
                arguments={},
                user=user,
                session_state=None,
                sig=sig,
                is_async=True,
                tool_name="get_file_content",
            )
        finally:
            set_current_correlation_id(None)  # type: ignore[arg-type]

        assert result == {"ok": True}
        assert observed["correlation_id"] == expected_correlation_id
