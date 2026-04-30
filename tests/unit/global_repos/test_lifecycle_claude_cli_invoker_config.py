"""
AC-V4-7 tests: LifecycleClaudeCliInvoker reads timeouts from ConfigService.

Verifies that:
1. The invoker reads outer_timeout_seconds from
   ConfigService.get_config().lifecycle_analysis_config at call time.
2. A config change between two calls (simulating Web UI hot-reload) produces
   updated timeout values on the second call — no module-level caching.

These tests patch build_dep_map_dispatcher (the external factory used by
_build_dispatcher) to intercept at the real external boundary rather than
mocking invoke_claude_cli, which is no longer called after the Bug #936
refactor to CliDispatcher.dispatch().
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


# ---------------------------------------------------------------------------
# Shared valid response body (matches unified prompt schema)
# ---------------------------------------------------------------------------

_VALID_RESPONSE_JSON = json.dumps(
    {
        "description": "A test repository.",
        "lifecycle": {
            "ci_system": "github-actions",
            "deployment_target": "pypi",
            "language_ecosystem": "python/poetry",
            "build_system": "poetry",
            "testing_framework": "pytest",
            "confidence": "high",
        },
    }
)


def _make_fake_server_config(shell_timeout: int, outer_timeout: int) -> MagicMock:
    """
    Build a minimal ServerConfig mock with lifecycle_analysis_config
    populated with the given timeout values.
    """
    lifecycle_cfg = MagicMock()
    lifecycle_cfg.shell_timeout_seconds = shell_timeout
    lifecycle_cfg.outer_timeout_seconds = outer_timeout

    server_config = MagicMock()
    server_config.lifecycle_analysis_config = lifecycle_cfg
    return server_config


def _make_success_dispatcher() -> MagicMock:
    """Return a mock dispatcher whose dispatch() returns a successful InvocationResult."""
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = InvocationResult(
        success=True,
        output=_VALID_RESPONSE_JSON,
        error="",
        cli_used="claude",
        was_failover=False,
    )
    return mock_dispatcher


# ---------------------------------------------------------------------------
# AC-V4-7 Test 1: invoker reads timeouts from ConfigService
# ---------------------------------------------------------------------------


def test_invoker_reads_timeouts_from_config_service(tmp_path: Path) -> None:
    """
    AC-V4-7: When ConfigService returns outer_timeout=650, the dispatcher
    must be called with timeout=650.

    Patches build_dep_map_dispatcher (the external factory) so the real
    _build_dispatcher control flow executes without calling real Claude.
    shell_timeout is embedded inside ClaudeInvoker and is not observable
    through the dispatch interface — only outer_timeout is asserted here.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "test-alias"
    repo_path.mkdir()

    server_config = _make_fake_server_config(shell_timeout=600, outer_timeout=650)
    expected_outer_timeout = (
        server_config.lifecycle_analysis_config.outer_timeout_seconds
    )

    mock_config_service = MagicMock()
    mock_config_service.get_config.return_value = server_config

    mock_dispatcher = _make_success_dispatcher()

    invoker = LifecycleClaudeCliInvoker()

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
            return_value=mock_config_service,
        ),
    ):
        invoker("test-alias", repo_path)

    assert mock_dispatcher.dispatch.called, "dispatch() must have been called"
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("timeout") == expected_outer_timeout, (
        f"Expected timeout={expected_outer_timeout} from ConfigService, "
        f"got {call_kwargs.get('timeout')}"
    )


# ---------------------------------------------------------------------------
# AC-V4-7 Test 2: hot-reload behavior — config change between calls
# ---------------------------------------------------------------------------


def test_invoker_reads_updated_timeouts_on_subsequent_call(tmp_path: Path) -> None:
    """
    AC-V4-7 hot-reload: After a Web UI config change between two calls,
    the second call must use the updated timeout value.

    Proves the invoker reads from ConfigService per-call, not at module
    load time. A module-level cached constant would fail this test.

    Patches build_dep_map_dispatcher with a side_effect that returns a
    fresh mock dispatcher on each call so per-call dispatch() kwargs are
    independently capturable.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "hot-reload-repo"
    repo_path.mkdir()

    # First call: config returns default 360/420
    first_config = _make_fake_server_config(shell_timeout=360, outer_timeout=420)
    first_outer_timeout = first_config.lifecycle_analysis_config.outer_timeout_seconds

    # Second call: config returns updated 600/650 (simulating Web UI save)
    second_config = _make_fake_server_config(shell_timeout=600, outer_timeout=650)
    second_outer_timeout = second_config.lifecycle_analysis_config.outer_timeout_seconds

    # Per-call dispatcher mocks so we can capture each dispatch() invocation's kwargs
    first_dispatcher = _make_success_dispatcher()
    second_dispatcher = _make_success_dispatcher()
    dispatchers = [first_dispatcher, second_dispatcher]

    config_call_count = 0

    def _mock_get_config():
        nonlocal config_call_count
        config_call_count += 1
        # Each invoker() call reads get_config() once for lifecycle_analysis_config
        # and once inside _build_dispatcher. Return first_config for the first
        # invoker() call and second_config for the second.
        if config_call_count <= 2:
            return first_config
        return second_config

    mock_config_service = MagicMock()
    mock_config_service.get_config.side_effect = _mock_get_config

    dispatcher_call_count = 0

    def _build_dispatcher_side_effect(_config):
        nonlocal dispatcher_call_count
        d = dispatchers[dispatcher_call_count]
        dispatcher_call_count += 1
        return d

    invoker = LifecycleClaudeCliInvoker()

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            side_effect=_build_dispatcher_side_effect,
        ),
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
            return_value=mock_config_service,
        ),
    ):
        # First call: should see outer_timeout from first_config
        invoker("hot-reload-repo", repo_path)
        # Second call: should see outer_timeout from second_config
        invoker("hot-reload-repo", repo_path)

    # Both dispatch() calls must have happened
    assert first_dispatcher.dispatch.called, "first dispatch() was not called"
    assert second_dispatcher.dispatch.called, "second dispatch() was not called"

    first_timeout = first_dispatcher.dispatch.call_args.kwargs.get("timeout")
    second_timeout = second_dispatcher.dispatch.call_args.kwargs.get("timeout")

    assert first_timeout == first_outer_timeout, (
        f"First call expected timeout={first_outer_timeout}, got {first_timeout}"
    )
    assert second_timeout == second_outer_timeout, (
        f"Second call (hot-reload) expected timeout={second_outer_timeout}, "
        f"got {second_timeout}"
    )
