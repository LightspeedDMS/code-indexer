"""Unit tests for asyncio.wait_for timeout wrapping in _invoke_handler.

Bug #1008: git_blame MCP tool times out on repos with deep history.

Fix 3: Wrap run_in_executor with asyncio.wait_for(timeout=60.0) so that
sync handlers that hang do not block the event loop indefinitely.

Issue #1190: Per-tool timeout override so exit_write_mode gets a timeout
comfortably above the 600s conflict-resolution budget while all other sync
handlers keep the 60s default.
"""

import asyncio
import inspect
import time
from datetime import datetime
from typing import Any, Dict
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import (
    _invoke_handler,
    _resolve_handler_timeout,
    HANDLER_TIMEOUT_SECONDS,
    WRITE_MODE_HANDLER_TIMEOUT_SECONDS,
    SEARCH_HANDLER_TIMEOUT_SECONDS,
)

EXPECTED_DEFAULT_TIMEOUT = 60


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
            "error": f"Tool execution timed out after {EXPECTED_DEFAULT_TIMEOUT} seconds",
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


class TestHandlerTimeoutResolver:
    """Tests for _resolve_handler_timeout — per-tool timeout override (Issue #1190)."""

    def test_exit_write_mode_gets_extended_timeout(self):
        """exit_write_mode must return WRITE_MODE_HANDLER_TIMEOUT_SECONDS, not the default.

        The sync exit_write_mode handler runs _execute_refresh() which invokes
        Claude-CLI conflict resolution capped at 600s. The protocol timeout must
        exceed that budget to avoid guillotining the operation at 60s.
        """
        timeout = _resolve_handler_timeout("exit_write_mode")
        assert timeout == WRITE_MODE_HANDLER_TIMEOUT_SECONDS
        assert timeout > HANDLER_TIMEOUT_SECONDS  # must exceed the default

    def test_git_blame_gets_default_timeout(self):
        """git_blame must keep the 60s default — Bug #1008 protection must stay intact."""
        timeout = _resolve_handler_timeout("git_blame")
        assert timeout == HANDLER_TIMEOUT_SECONDS

    def test_arbitrary_tool_gets_default_timeout(self):
        """Any tool without an explicit override must get HANDLER_TIMEOUT_SECONDS."""
        for tool_name in ("get_file_content", "list_files", "git_log"):
            timeout = _resolve_handler_timeout(tool_name)
            assert timeout == HANDLER_TIMEOUT_SECONDS, (
                f"Expected default {HANDLER_TIMEOUT_SECONDS}s for {tool_name!r}, "
                f"got {timeout}s"
            )

    def test_write_mode_timeout_exceeds_conflict_resolution_budget(self):
        """WRITE_MODE_HANDLER_TIMEOUT_SECONDS must be >= 720 (600s conflict budget + 120s buffer).

        _DEFAULT_CONFLICT_TIMEOUT in conflict_resolver.py is 600s. The protocol
        override must give at least 120s of headroom for surrounding git work.
        """
        assert WRITE_MODE_HANDLER_TIMEOUT_SECONDS >= 720

    def test_search_code_gets_extended_timeout(self):
        """search_code must return SEARCH_HANDLER_TIMEOUT_SECONDS, not the 60s default.

        Bug #1319: temporal search_code queries (query embed + HNSW over many
        quarterly shards + hydration + reranking) legitimately take ~13-20s and
        the tail exceeds 60s under concurrent load, even though the query is
        correct and eventually completes. Uses the existing per-tool override
        mechanism (Issue #1190) rather than raising the global default.
        """
        timeout = _resolve_handler_timeout("search_code")
        assert timeout == SEARCH_HANDLER_TIMEOUT_SECONDS
        assert timeout == 180
        assert timeout > HANDLER_TIMEOUT_SECONDS  # must exceed the default


class TestInvokeHandlerTimeoutParam:
    """Tests for _invoke_handler honouring the injected timeout_seconds parameter."""

    @pytest.mark.asyncio
    async def test_invoke_handler_uses_injected_timeout_on_timeout(self):
        """When a tiny timeout_seconds is injected, a slow sync handler must time out
        and the error message must report the INJECTED timeout, not the default 60s.

        Uses a real asyncio.wait_for with a 0.1s timeout and a handler that sleeps
        0.5s — ensures we exercise the real timeout path without sleeping 60s.
        """

        def slow_handler(arguments, user):
            time.sleep(0.5)  # longer than injected timeout
            return {"ok": True}

        user = _make_user()
        sig = inspect.signature(slow_handler)
        injected_timeout = 0.1  # 100 ms — fast, but provably times out in CI

        result = await _invoke_handler(
            handler=slow_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=injected_timeout,
        )

        assert result == {
            "success": False,
            "error": f"Tool execution timed out after {injected_timeout} seconds",
        }

    @pytest.mark.asyncio
    async def test_invoke_handler_default_timeout_unchanged(self):
        """When timeout_seconds is not supplied, the default path is byte-identical.

        A fast handler must still return its result normally — the default parameter
        must preserve backward compatibility for all current callers.
        """

        def fast_handler(arguments, user):
            return {"result": "ok"}

        user = _make_user()
        sig = inspect.signature(fast_handler)

        result = await _invoke_handler(
            handler=fast_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            # timeout_seconds NOT supplied — must use HANDLER_TIMEOUT_SECONDS default
        )

        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_invoke_handler_extended_timeout_allows_slow_handler(self):
        """When a generous timeout is injected, a slightly-slow handler must complete.

        Proves that the timeout_seconds parameter is genuinely honoured (not ignored)
        by showing a 0.2s sleep completes under a 2s timeout.
        """

        def slightly_slow_handler(arguments, user):
            time.sleep(0.2)
            return {"done": True}

        user = _make_user()
        sig = inspect.signature(slightly_slow_handler)

        result = await _invoke_handler(
            handler=slightly_slow_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=2.0,  # generous — handler finishes in 0.2s
        )

        assert result == {"done": True}


class TestHandleToolsCallTimeoutThreading:
    """Anti-regression: handle_tools_call must thread the resolved timeout into
    _invoke_handler at BOTH call sites (langfuse-wrapped path AND direct path).

    A future refactor that drops timeout_seconds=handler_timeout at either site
    would silently revert exit_write_mode to the 60s default, guillotining
    Claude-CLI conflict resolution at 60s instead of the budgeted 720s.

    Strategy: patch _invoke_handler as a spy so we can inspect the keyword
    arguments it receives for each call site. Stub out TOOL_REGISTRY,
    HANDLER_REGISTRY, access-service, and langfuse with minimal fakes — no
    network, no DB.
    """

    # ---------------------------------------------------------------------------
    # Shared helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _make_user_with_perms() -> MagicMock:
        """Return a user mock whose has_permission always returns True."""
        user = _make_user()
        m = MagicMock(spec=user)
        m.username = user.username
        m.has_permission = MagicMock(return_value=True)
        return m

    @staticmethod
    def _make_fake_registries() -> tuple:
        """Return (fake_tool_registry, fake_handlers_module)."""

        def _dummy_handler(arguments: Dict[str, Any], user: Any) -> Dict[str, Any]:
            return {"ok": True}

        fake_tool_registry: Dict[str, Any] = {
            "exit_write_mode": {"required_permission": "read"},
            "get_file_content": {"required_permission": "read"},
        }
        fake_handler_registry: Dict[str, Any] = {
            "exit_write_mode": _dummy_handler,
            "get_file_content": _dummy_handler,
        }
        fake_handlers_module = MagicMock(
            HANDLER_REGISTRY=fake_handler_registry,
            app_module=MagicMock(
                app=MagicMock(
                    state=MagicMock(
                        access_filtering_service=MagicMock(
                            is_admin_user=MagicMock(return_value=True),
                            get_accessible_repos=MagicMock(return_value=set()),
                        )
                    )
                )
            ),
        )
        return fake_tool_registry, fake_handlers_module

    @staticmethod
    def _make_timeout_spy() -> tuple:
        """Return (captured_timeouts dict, async spy coroutine)."""
        captured_timeouts: Dict[int, float] = {}
        call_count = [0]

        async def _spy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            idx = call_count[0]
            captured_timeouts[idx] = kwargs.get("timeout_seconds", -1)
            call_count[0] += 1
            return {"ok": True}

        return captured_timeouts, _spy

    @staticmethod
    def _make_sys_modules_patch(
        fake_tool_registry: Dict[str, Any],
        fake_handlers_module: Any,
    ) -> dict:
        """Return the sys.modules patch dict for lazy imports inside handle_tools_call."""
        return {
            "code_indexer.server.mcp.handlers": fake_handlers_module,
            "code_indexer.server.mcp.tools": MagicMock(
                TOOL_REGISTRY=fake_tool_registry
            ),
            "code_indexer.server.mcp.session_registry": MagicMock(
                get_session_registry=MagicMock(
                    return_value=MagicMock(
                        get_or_create_session=MagicMock(return_value=None)
                    )
                )
            ),
        }

    # ---------------------------------------------------------------------------
    # Direct path (langfuse=None)
    # ---------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_direct_path_threads_resolved_timeout(self) -> None:
        """Direct dispatch path: timeout_seconds must equal the resolved value.

        When get_langfuse_service() returns None, handle_tools_call falls into the
        else-branch and calls _invoke_handler directly. Asserts:
        - exit_write_mode  -> WRITE_MODE_HANDLER_TIMEOUT_SECONDS (720)
        - get_file_content -> HANDLER_TIMEOUT_SECONDS (60)
        """
        from code_indexer.server.mcp import protocol as protocol_module

        user = self._make_user_with_perms()
        fake_tool_registry, fake_handlers_module = self._make_fake_registries()
        captured_timeouts, spy = self._make_timeout_spy()
        sys_patch = self._make_sys_modules_patch(
            fake_tool_registry, fake_handlers_module
        )

        with (
            patch.object(protocol_module, "_invoke_handler", side_effect=spy),
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,  # direct path
            ),
            patch.dict("sys.modules", sys_patch),
        ):
            await protocol_module.handle_tools_call(
                params={"name": "exit_write_mode", "arguments": {}},
                user=user,
            )
            await protocol_module.handle_tools_call(
                params={"name": "get_file_content", "arguments": {}},
                user=user,
            )

        assert captured_timeouts[0] == WRITE_MODE_HANDLER_TIMEOUT_SECONDS, (
            f"direct path / exit_write_mode: expected {WRITE_MODE_HANDLER_TIMEOUT_SECONDS}, "
            f"got {captured_timeouts[0]}"
        )
        assert captured_timeouts[1] == HANDLER_TIMEOUT_SECONDS, (
            f"direct path / get_file_content: expected {HANDLER_TIMEOUT_SECONDS}, "
            f"got {captured_timeouts[1]}"
        )

    # ---------------------------------------------------------------------------
    # Langfuse-wrapped path (langfuse service present)
    # ---------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_langfuse_path_threads_resolved_timeout(self) -> None:
        """Langfuse-wrapped dispatch path: timeout_seconds must equal the resolved value.

        When get_langfuse_service() returns a service, handle_tools_call wraps the
        call in a langfuse span interceptor that itself awaits handler_wrapper(),
        which calls _invoke_handler. Asserts both tools receive the correct timeout
        via that wrapped call site:
        - exit_write_mode  -> WRITE_MODE_HANDLER_TIMEOUT_SECONDS (720)
        - get_file_content -> HANDLER_TIMEOUT_SECONDS (60)
        """
        from code_indexer.server.mcp import protocol as protocol_module

        user = self._make_user_with_perms()
        fake_tool_registry, fake_handlers_module = self._make_fake_registries()
        captured_timeouts, spy = self._make_timeout_spy()
        sys_patch = self._make_sys_modules_patch(
            fake_tool_registry, fake_handlers_module
        )

        # Fake langfuse service whose interceptor simply awaits the handler coroutine.
        async def _fake_intercept(
            session_id: Any,
            tool_name: Any,
            arguments: Any,
            handler: Any,
            username: Any,
        ) -> Any:
            return await handler()

        fake_span_logger = MagicMock()
        fake_span_logger.intercept_tool_call = _fake_intercept
        fake_langfuse_service = MagicMock()
        fake_langfuse_service.span_logger = fake_span_logger

        with (
            patch.object(protocol_module, "_invoke_handler", side_effect=spy),
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=fake_langfuse_service,  # langfuse-wrapped path
            ),
            patch.dict("sys.modules", sys_patch),
        ):
            await protocol_module.handle_tools_call(
                params={"name": "exit_write_mode", "arguments": {}},
                user=user,
            )
            await protocol_module.handle_tools_call(
                params={"name": "get_file_content", "arguments": {}},
                user=user,
            )

        assert captured_timeouts[0] == WRITE_MODE_HANDLER_TIMEOUT_SECONDS, (
            f"langfuse path / exit_write_mode: expected {WRITE_MODE_HANDLER_TIMEOUT_SECONDS}, "
            f"got {captured_timeouts[0]}"
        )
        assert captured_timeouts[1] == HANDLER_TIMEOUT_SECONDS, (
            f"langfuse path / get_file_content: expected {HANDLER_TIMEOUT_SECONDS}, "
            f"got {captured_timeouts[1]}"
        )
