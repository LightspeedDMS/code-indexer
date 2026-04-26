"""
AC-V4-7 tests: LifecycleClaudeCliInvoker reads timeouts from ConfigService.

Verifies that:
1. The invoker reads shell_timeout_seconds and outer_timeout_seconds from
   ConfigService.get_config().lifecycle_analysis_config at call time.
2. A config change between two calls (simulating Web UI hot-reload) produces
   updated timeout values on the second call — no module-level caching.

These tests are RED until LifecycleAnalysisConfig is added to config_manager.py
and lifecycle_claude_cli_invoker.py is refactored to read from ConfigService.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple
from unittest.mock import MagicMock, patch



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


# ---------------------------------------------------------------------------
# AC-V4-7 Test 1: invoker reads timeouts from ConfigService
# ---------------------------------------------------------------------------


def test_invoker_reads_timeouts_from_config_service(tmp_path: Path) -> None:
    """
    AC-V4-7: When ConfigService returns shell_timeout=600 and outer_timeout=650,
    invoke_claude_cli must be called with those exact values.

    This is RED until the invoker is refactored away from module-level constants.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "test-alias"
    repo_path.mkdir()

    captured: dict = {}

    def _fake_invoke(
        rp: str, prompt: str, shell_timeout: int, outer_timeout: int
    ) -> Tuple[bool, str]:
        captured["shell_timeout"] = shell_timeout
        captured["outer_timeout"] = outer_timeout
        return True, _VALID_RESPONSE_JSON

    mock_config_service = MagicMock()
    mock_config_service.get_config.return_value = _make_fake_server_config(
        shell_timeout=600, outer_timeout=650
    )

    invoker = LifecycleClaudeCliInvoker()

    with patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.invoke_claude_cli",
        side_effect=_fake_invoke,
    ), patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
        return_value=mock_config_service,
    ):
        invoker("test-alias", repo_path)

    assert captured["shell_timeout"] == 600, (
        f"Expected shell_timeout=600 from ConfigService, got {captured['shell_timeout']}"
    )
    assert captured["outer_timeout"] == 650, (
        f"Expected outer_timeout=650 from ConfigService, got {captured['outer_timeout']}"
    )


# ---------------------------------------------------------------------------
# AC-V4-7 Test 2: hot-reload behavior — config change between calls
# ---------------------------------------------------------------------------


def test_invoker_reads_updated_timeouts_on_subsequent_call(tmp_path: Path) -> None:
    """
    AC-V4-7 hot-reload: After a Web UI config change between two calls,
    the second call must use the updated timeout values.

    This proves the invoker reads from ConfigService per-call, not at module
    load time. A module-level cached constant would fail this test.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "hot-reload-repo"
    repo_path.mkdir()

    call_captures: list = []

    def _fake_invoke(
        rp: str, prompt: str, shell_timeout: int, outer_timeout: int
    ) -> Tuple[bool, str]:
        call_captures.append(
            {"shell_timeout": shell_timeout, "outer_timeout": outer_timeout}
        )
        return True, _VALID_RESPONSE_JSON

    # First call: config returns default 360/420
    first_config = _make_fake_server_config(shell_timeout=360, outer_timeout=420)
    # Second call: config returns updated 600/650 (simulating Web UI save)
    second_config = _make_fake_server_config(shell_timeout=600, outer_timeout=650)

    call_count = 0

    def _mock_get_config():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return first_config
        return second_config

    mock_config_service = MagicMock()
    mock_config_service.get_config.side_effect = _mock_get_config

    invoker = LifecycleClaudeCliInvoker()

    with patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.invoke_claude_cli",
        side_effect=_fake_invoke,
    ), patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
        return_value=mock_config_service,
    ):
        # First call: should see 360/420
        invoker("hot-reload-repo", repo_path)
        # Second call: should see updated 600/650
        invoker("hot-reload-repo", repo_path)

    assert len(call_captures) == 2

    # First call saw the original timeouts
    assert call_captures[0]["shell_timeout"] == 360, (
        f"First call expected shell_timeout=360, got {call_captures[0]['shell_timeout']}"
    )
    assert call_captures[0]["outer_timeout"] == 420, (
        f"First call expected outer_timeout=420, got {call_captures[0]['outer_timeout']}"
    )

    # Second call saw the updated timeouts — hot-reload verified
    assert call_captures[1]["shell_timeout"] == 600, (
        f"Second call expected shell_timeout=600 (hot-reload), got {call_captures[1]['shell_timeout']}"
    )
    assert call_captures[1]["outer_timeout"] == 650, (
        f"Second call expected outer_timeout=650 (hot-reload), got {call_captures[1]['outer_timeout']}"
    )
