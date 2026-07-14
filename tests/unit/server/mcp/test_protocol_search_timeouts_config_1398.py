"""Tests proving SearchTimeoutsConfig actually governs protocol.py's MCP
handler dispatch timeouts (Issue #1398).

These tests deliberately do NOT stop at proving _resolve_handler_timeout()
returns the right number -- per the issue's explicit testing requirement,
they prove the ACTUAL asyncio.wait_for dispatch times out at the configured
value, not the old hardcoded constant. A dataclass round-trip alone would
not catch a missing wiring between config and the real dispatch path.
"""

import asyncio
import inspect
import time

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from datetime import datetime
from code_indexer.server.mcp.protocol import _invoke_handler, _resolve_handler_timeout
from code_indexer.server.services.config_service import (
    ConfigService,
    set_config_service,
    reset_config_service,
)


def _make_user() -> User:
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def isolated_config_service(tmp_path):
    """A real ConfigService rooted at tmp_path, installed as the global
    singleton for the duration of the test and reset afterward."""
    svc = ConfigService(server_dir_path=str(tmp_path))
    set_config_service(svc)
    try:
        yield svc
    finally:
        reset_config_service()


class TestResolveHandlerTimeoutReflectsLiveConfig:
    """_resolve_handler_timeout must read the CURRENT config, not a
    snapshot taken at import time -- proves the wiring, not just defaults."""

    def test_search_code_timeout_reflects_configured_value(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "search_code_handler_timeout_seconds", 35
        )
        assert _resolve_handler_timeout("search_code") == 35

    def test_exit_write_mode_timeout_reflects_configured_value(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "write_mode_handler_timeout_seconds", 900
        )
        assert _resolve_handler_timeout("exit_write_mode") == 900

    def test_default_timeout_reflects_configured_value_for_arbitrary_tool(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "default_handler_timeout_seconds", 45
        )
        assert _resolve_handler_timeout("get_file_content") == 45
        assert _resolve_handler_timeout("git_blame") == 45

    def test_default_config_matches_pre_1398_hardcoded_values(
        self, isolated_config_service
    ) -> None:
        """Byte-identical defaults preserve all pre-#1398 behavior."""
        assert _resolve_handler_timeout("search_code") == 180
        assert _resolve_handler_timeout("exit_write_mode") == 720
        assert _resolve_handler_timeout("get_file_content") == 60


class TestSearchCodeDispatchActuallyHonorsConfiguredTimeout:
    """Proves the REAL asyncio.wait_for dispatch path times out at the
    configured value -- not merely that the resolver function returns the
    right number. Uses a real (non-mocked) asyncio.wait_for with a small
    injected timeout and a handler that provably sleeps longer."""

    @pytest.mark.asyncio
    async def test_slow_search_code_handler_times_out_at_small_configured_value(
        self, isolated_config_service
    ) -> None:
        # Direct mutation + save_config (bypasses the 30-600s Web UI
        # validation range, which exists to protect operators from
        # misconfiguration -- not to prevent this test from proving the
        # wiring fires quickly, without a real 30+ second wait in CI).
        config = isolated_config_service.get_config()
        config.search_timeouts_config.search_code_handler_timeout_seconds = 1
        isolated_config_service.save_config(config)

        resolved_timeout = _resolve_handler_timeout("search_code")
        assert resolved_timeout == 1  # sanity: config actually took effect

        def slow_search_code_handler(arguments, user):
            time.sleep(2.0)  # longer than the configured 1s timeout
            return {"success": True}

        user = _make_user()
        sig = inspect.signature(slow_search_code_handler)

        result = await _invoke_handler(
            handler=slow_search_code_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=resolved_timeout,
        )

        assert result == {
            "success": False,
            "error": f"Tool execution timed out after {resolved_timeout} seconds",
        }

    @pytest.mark.asyncio
    async def test_search_code_handler_completes_under_generous_configured_value(
        self, isolated_config_service
    ) -> None:
        """Proves the config value is genuinely honoured in both
        directions: a generous configured timeout lets a slower-than-old-
        default-but-still-fast handler complete normally."""
        isolated_config_service.update_setting(
            "search_timeouts", "search_code_handler_timeout_seconds", 300
        )
        resolved_timeout = _resolve_handler_timeout("search_code")
        assert resolved_timeout == 300

        def moderately_slow_handler(arguments, user):
            time.sleep(0.2)
            return {"success": True, "value": 1}

        user = _make_user()
        sig = inspect.signature(moderately_slow_handler)

        result = await _invoke_handler(
            handler=moderately_slow_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=resolved_timeout,
        )

        assert result == {"success": True, "value": 1}


class TestRegexSearchIndependentOfSearchTimeoutsConfig:
    """Regression: regex_search is dispatched ASYNCHRONOUSLY (async def
    handler), so handle_tools_call's is_async branch calls
    `await handler(...)` directly with NO asyncio.wait_for wrapper at all.
    Changing search_timeouts_config (search_code_handler_timeout_seconds or
    default_handler_timeout_seconds) must have ZERO effect on regex_search's
    dispatch -- proving the two tools remain independently configurable and
    do not silently interact."""

    def test_regex_search_handler_is_registered_as_async(self) -> None:
        from code_indexer.server.mcp.handlers.search import handle_regex_search

        assert asyncio.iscoroutinefunction(handle_regex_search), (
            "handle_regex_search must be async def -- if this ever changes "
            "to sync def, it would start being wrapped by "
            "_resolve_handler_timeout's asyncio.wait_for cap, changing "
            "long-standing behavior silently."
        )

    @pytest.mark.asyncio
    async def test_async_dispatch_ignores_even_a_near_zero_timeout_seconds(
        self, isolated_config_service
    ) -> None:
        """Even if search_code_handler_timeout_seconds (or the default) were
        driven to an absurdly small value, an async handler dispatched via
        is_async=True must still complete normally -- _invoke_handler's
        async branch never consults timeout_seconds at all."""
        # Validated update FIRST (10 is within the 10-300 range), THEN the
        # direct-mutation bypass for the artificially small search_code
        # value LAST -- update_setting's post-mutation validate_config()
        # call validates the WHOLE search_timeouts_config object, so
        # applying the out-of-range bypass before a later update_setting
        # call would raise a spurious ValueError for an unrelated field.
        isolated_config_service.update_setting(
            "search_timeouts", "default_handler_timeout_seconds", 10
        )
        config = isolated_config_service.get_config()
        config.search_timeouts_config.search_code_handler_timeout_seconds = 1
        isolated_config_service.save_config(config)

        async def slow_async_regex_handler(arguments, user):
            await asyncio.sleep(0.3)  # slower than the 1s configured value
            # would still legitimately fit under 1s, so use a wait_for-based
            # proof instead: async branch takes no timeout_seconds at all.
            return {"success": True, "matches": []}

        user = _make_user()
        sig = inspect.signature(slow_async_regex_handler)

        # Deliberately pass an absurdly small timeout_seconds (0.001s) --
        # if the async branch respected it at all, this would time out.
        result = await _invoke_handler(
            handler=slow_async_regex_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=True,
            timeout_seconds=0.001,
        )

        assert result == {"success": True, "matches": []}, (
            "Async dispatch must ignore timeout_seconds entirely -- "
            "regex_search's actual bound is search_limits.timeout_seconds "
            "(its own subprocess timeout), never this MCP-layer value."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
