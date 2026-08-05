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
    singleton for the duration of the test and reset afterward.

    Isolation hardening: several tests below import
    code_indexer.server.mcp.handlers.search (directly or transitively), which
    imports code_indexer.server.app. app.py runs `app = create_app()` at
    MODULE scope, and create_app() -> initialize_services() ->
    ConfigService.initialize_runtime_db() performs an atomic
    `self._config = new_config` reference swap on the CURRENT global config
    singleton. If that one-time import fires AFTER we install our isolated
    ConfigService and apply update_setting(...), it silently WIPES our
    configured values back to defaults -- the source of this file's
    config-singleton flakiness under parallel/full-suite load (which import
    fires first depends on test distribution). Force the import-time side
    effect to happen NOW -- against the pre-existing global config, before we
    install ours -- so the svc we set up next is never clobbered. Once app is
    in sys.modules, later imports are no-ops."""
    import code_indexer.server.app  # noqa: F401  -- import-time side effect only

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
        # Story #1400 CRITICAL 5: temporal_inline_wait_seconds must leave a
        # >=1s grace below search_code_handler_timeout_seconds. The default
        # temporal_inline_wait_seconds (60.0) is incompatible with 35, so
        # lower it first -- this is the new, intended cross-field invariant,
        # not a workaround.
        isolated_config_service.update_setting(
            "search_timeouts", "temporal_inline_wait_seconds", 5.0
        )
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


_NEAR_ZERO_DEFAULT_HANDLER_TIMEOUT_SECONDS = 10
_NEAR_ZERO_SEARCH_CODE_TIMEOUT_SECONDS = 1
_CONFIGURED_REGEX_SUBPROCESS_TIMEOUT_SECONDS = 200
_SHORT_ASYNC_DEADLINE_SECONDS = 0.05
_LONGER_THAN_DEADLINE_SLEEP_SECONDS = 10

# Story #1491 AC6 code-review fix: regex_search is exempt from the async
# deadline entirely, so these constants exist only to prove a slow/
# multi-repo-shaped handler is never killed early -- not to derive a cap.
_REGEX_EXEMPT_SLEEP_SECONDS = 0.1
_REGEX_OMNI_SINGLE_REPO_FLOOR_SECONDS = 0.05
_REGEX_OMNI_REPO_COUNT = 3
_REGEX_OMNI_DEADLINE_BUFFER_SECONDS = 0.02


class TestRegexSearchIndependentOfSearchTimeoutsConfig:
    """Regression: regex_search is dispatched ASYNCHRONOUSLY (async def
    handler). Story #1491 AC6 (Finding B6) added a real asyncio.wait_for
    deadline to the async dispatch branch -- but regex_search is EXEMPT
    from it (code-review correction, round 2): _omni_regex_search loops
    repos SEQUENTIALLY, each bounded by its own
    search_limits_config.timeout_seconds subprocess call, so an omni
    search over N repos has legitimate cumulative runtime of
    N x single-repo-floor. No single fixed outer deadline (however
    derived) can accommodate an unbounded N without either being too
    tight for a real multi-repo omni search or too loose to be a
    meaningful deadline at all. Per this project's own documented
    invariant (CLAUDE.md Issue #1398): "regex_search is async-dispatched
    (governed entirely by search_limits_config.timeout_seconds + its own
    subprocess timeout ...) -- the two are independently configurable and
    NEVER interact." AC6's new deadline must not violate that."""

    def test_regex_search_is_sync_dispatched_and_still_deadline_exempt(self) -> None:
        """Story #1491 AC2: regex_search is now SYNC-dispatched, deliberately.

        The registered handler was changed from the async implementation to the
        plain-``def`` ``handle_regex_search_sync`` so the dispatcher's
        ``run_in_executor`` branch takes the handler's synchronous work
        (trigram prefilter, Path.resolve fan-out, ripgrep output read +
        json.loads) OFF the event loop (report Finding B2).

        The timeout invariant this class exists to protect is unchanged, and
        that is exactly what is asserted here: ``regex_search`` remains a
        member of ``_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS``, which the
        dispatcher honours on BOTH branches, so the tool is still governed
        solely by ``search_limits_config.timeout_seconds`` plus its own
        subprocess timeout -- never by a handler-level cap. If a future change
        drops the exemption or re-registers the coroutine, this fails.
        """
        from code_indexer.server.mcp.handlers import search as search_handlers
        from code_indexer.server.mcp.protocol import (
            _ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS,
        )

        registry: dict = {}
        search_handlers._register(registry)
        registered = registry["regex_search"]

        assert not asyncio.iscoroutinefunction(registered), (
            "the registered regex_search handler must be a plain def so the "
            "dispatcher offloads it to the executor (Story #1491 AC2)"
        )
        assert registered is search_handlers.handle_regex_search_sync
        assert asyncio.iscoroutinefunction(search_handlers.handle_regex_search), (
            "the async implementation must remain a coroutine -- "
            "_omni_regex_search awaits it per repo"
        )
        assert "regex_search" in _ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS, (
            "regex_search must stay deadline-exempt: sync dispatch must not "
            "silently impose an MCP-layer cap it never had"
        )

    @pytest.mark.asyncio
    async def test_regex_search_async_dispatch_is_exempt_from_ac6_deadline(
        self,
    ) -> None:
        """tool_name="regex_search" must bypass the AC6 wait_for wrapper
        entirely -- an arbitrarily small timeout_seconds must have ZERO
        effect when the handler is dispatched as regex_search."""

        async def slow_regex_handler(arguments, user):
            await asyncio.sleep(_REGEX_EXEMPT_SLEEP_SECONDS)
            return {"success": True, "matches": []}

        user = _make_user()
        sig = inspect.signature(slow_regex_handler)

        result = await _invoke_handler(
            handler=slow_regex_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=True,
            timeout_seconds=_SHORT_ASYNC_DEADLINE_SECONDS,
            tool_name="regex_search",
        )

        assert result == {"success": True, "matches": []}, (
            "regex_search must be exempt from the AC6 async deadline -- "
            "a slow handler must complete normally regardless of "
            "timeout_seconds."
        )

    @pytest.mark.asyncio
    async def test_regex_search_omni_style_cumulative_time_exceeding_single_repo_floor_is_not_killed(
        self,
    ) -> None:
        """Discriminating test (reviewer-requested): simulates
        _omni_regex_search's real sequential-per-repo loop shape -- N
        repos, each bounded by its own single-repo timeout floor -- with a
        cumulative total that EXCEEDS a deadline shaped like the old,
        now-removed buggy "single-repo-derived" cap (a single floor plus a
        fixed buffer). Proves the omni-style handler is not killed early."""
        single_repo_derived_deadline = (
            _REGEX_OMNI_SINGLE_REPO_FLOOR_SECONDS + _REGEX_OMNI_DEADLINE_BUFFER_SECONDS
        )

        async def omni_style_regex_handler(arguments, user):
            for _ in range(_REGEX_OMNI_REPO_COUNT):
                await asyncio.sleep(_REGEX_OMNI_SINGLE_REPO_FLOOR_SECONDS)
            return {
                "success": True,
                "matches": [],
                "repos_searched": _REGEX_OMNI_REPO_COUNT,
            }

        user = _make_user()
        sig = inspect.signature(omni_style_regex_handler)

        result = await _invoke_handler(
            handler=omni_style_regex_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=True,
            timeout_seconds=single_repo_derived_deadline,
            tool_name="regex_search",
        )

        assert result == {
            "success": True,
            "matches": [],
            "repos_searched": _REGEX_OMNI_REPO_COUNT,
        }, (
            "An omni-style regex search whose cumulative sequential "
            "runtime exceeds a single-repo-derived deadline must still "
            "complete successfully -- it must never be killed early."
        )

    @pytest.mark.asyncio
    async def test_async_dispatch_now_respects_a_real_deadline_per_ac6(
        self,
    ) -> None:
        """Story #1491 AC6: the async branch now genuinely times out a
        handler that outlives its deadline -- proving the OLD "async
        dispatch never applies any timeout" behavior is gone."""

        async def slow_async_handler(arguments, user):
            await asyncio.sleep(_LONGER_THAN_DEADLINE_SLEEP_SECONDS)
            return {"success": True, "matches": []}

        user = _make_user()
        sig = inspect.signature(slow_async_handler)

        result = await _invoke_handler(
            handler=slow_async_handler,
            arguments={},
            user=user,
            session_state=None,
            sig=sig,
            is_async=True,
            timeout_seconds=_SHORT_ASYNC_DEADLINE_SECONDS,
        )

        assert result["success"] is False
        assert "timed out" in result["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
