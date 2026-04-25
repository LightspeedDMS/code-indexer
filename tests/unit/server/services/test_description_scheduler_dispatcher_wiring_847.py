"""
Tests for CliDispatcher wiring into DescriptionRefreshScheduler (Story #847).

These tests verify that DescriptionRefreshScheduler._invoke_claude_cli routes
through a CliDispatcher and not the legacy direct-Claude invoke_claude_cli path.

Test inventory (6 tests):
    Test 1 — scheduler builds a CliDispatcher with codex=None when
              codex_integration_config.enabled == False.
    Test 2 — scheduler builds dispatcher with both invokers + codex_weight
              from config when codex is enabled.
    Test 3 — scheduler-execution drives dispatcher.dispatch(...) with the
              exact flow="description_refresh", cwd=repo_path, prompt=prompt,
              timeout=_CLAUDE_CLI_HARD_TIMEOUT_SECONDS (120).
    Test 4a — success: result.output flows back as (True, output_str).
    Test 4b — failure: result.error flows back as (False, error_str).
    Test 5 — failover logging: when result.was_failover=True an INFO log
              captures cli_used value and explicit was_failover indicator.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

# The scheduler hard-timeout constant — the dispatcher must receive this exact value.
_EXPECTED_TIMEOUT = 120  # _CLAUDE_CLI_HARD_TIMEOUT_SECONDS in description_refresh_scheduler.py
_EXPECTED_FLOW = "description_refresh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_result(cli_used: str = "claude", was_failover: bool = False) -> InvocationResult:
    # Output must exceed 100 chars to pass _validate_cli_output's minimum-length check.
    return InvocationResult(
        success=True,
        output=(
            "---\ntitle: Test Repo\n---\n"
            "A comprehensive test repository providing utilities for code indexing "
            "and semantic search across large codebases."
        ),
        error="",
        cli_used=cli_used,
        was_failover=was_failover,
    )


def _make_failure_result(cli_used: str = "claude") -> InvocationResult:
    from code_indexer.server.services.intelligence_cli_invoker import FailureClass

    return InvocationResult(
        success=False,
        output="",
        error="CLI exploded",
        cli_used=cli_used,
        was_failover=False,
        failure_class=FailureClass.RETRYABLE_ON_OTHER,
    )


def _make_mock_config(codex_enabled: bool = False, codex_weight: float = 0.4):
    """Build a minimal mock ServerConfig for the scheduler."""
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        CodexIntegrationConfig,
    )

    claude_cfg = ClaudeIntegrationConfig(
        description_refresh_enabled=True,
        max_concurrent_claude_cli=1,
        description_refresh_interval_hours=24,
    )
    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )

    cfg = MagicMock()
    cfg.claude_integration_config = claude_cfg
    cfg.codex_integration_config = codex_cfg
    return cfg


def _make_scheduler(cli_dispatcher=None, config=None):
    """Build a DescriptionRefreshScheduler with injectable backends and config."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    tracking_backend = MagicMock()
    golden_backend = MagicMock()
    config_manager = MagicMock()
    config_manager.load_config.return_value = config or _make_mock_config()

    kwargs: dict = dict(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=config_manager,
        analysis_model="opus",
    )
    if cli_dispatcher is not None:
        kwargs["cli_dispatcher"] = cli_dispatcher

    return DescriptionRefreshScheduler(**kwargs)


def _get_dispatch_arg(call_obj, name: str, positional_index: int):
    """
    Safely extract a named or positional argument from a mock call object.

    Checks kwargs first; falls back to args only when the positional index
    is within the actual args tuple length to avoid IndexError.
    """
    if name in call_obj.kwargs:
        return call_obj.kwargs[name]
    if positional_index < len(call_obj.args):
        return call_obj.args[positional_index]
    raise AssertionError(
        f"Argument '{name}' (positional index {positional_index}) not found in call: "
        f"args={call_obj.args!r}, kwargs={call_obj.kwargs!r}"
    )


# ---------------------------------------------------------------------------
# Test 1: codex disabled → CliDispatcher built with codex=None
# ---------------------------------------------------------------------------


class TestSchedulerBuildsDispatcherCodexDisabled:
    """When codex_integration_config.enabled is False, codex arg is None."""

    def test_dispatcher_built_with_codex_none_when_codex_disabled(self) -> None:
        """
        DescriptionRefreshScheduler._build_cli_dispatcher returns a CliDispatcher
        whose .codex attribute is None when config.codex_integration_config.enabled
        is False.
        """
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        config = _make_mock_config(codex_enabled=False, codex_weight=0.5)
        scheduler = _make_scheduler(config=config)
        dispatcher = scheduler._build_cli_dispatcher(config)

        assert isinstance(dispatcher, CliDispatcher)
        # codex is None → effective codex_weight collapses to 0.0 inside CliDispatcher
        assert dispatcher.codex is None
        assert dispatcher.codex_weight == 0.0


# ---------------------------------------------------------------------------
# Test 2: codex enabled → both invokers + codex_weight from config
# ---------------------------------------------------------------------------


class TestSchedulerBuildsDispatcherCodexEnabled:
    """When codex is enabled, both invokers are wired and weight matches config."""

    def test_dispatcher_built_with_both_invokers_when_codex_enabled(self) -> None:
        """
        _build_cli_dispatcher with codex enabled produces a CliDispatcher
        where both .claude and .codex are non-None, and .codex_weight matches
        the value in codex_integration_config.
        """
        from code_indexer.server.services.cli_dispatcher import CliDispatcher
        from code_indexer.server.services.claude_invoker import ClaudeInvoker
        from code_indexer.server.services.codex_invoker import CodexInvoker

        config = _make_mock_config(codex_enabled=True, codex_weight=0.3)
        scheduler = _make_scheduler(config=config)

        with patch.dict("os.environ", {"CODEX_HOME": "/fake/codex-home"}):
            dispatcher = scheduler._build_cli_dispatcher(config)

        assert isinstance(dispatcher, CliDispatcher)
        assert isinstance(dispatcher.claude, ClaudeInvoker)
        assert isinstance(dispatcher.codex, CodexInvoker)
        assert dispatcher.codex_weight == 0.3


# ---------------------------------------------------------------------------
# Test 3: dispatch called with exact flow/cwd/prompt/timeout values
# ---------------------------------------------------------------------------


class TestSchedulerDispatchCallArgs:
    """_invoke_claude_cli must call dispatcher.dispatch with exact argument values."""

    def test_dispatch_called_with_correct_args(self) -> None:
        """
        When _invoke_claude_cli is called with a repo_path and prompt,
        the injected dispatcher receives:
          flow  = "description_refresh"
          cwd   = repo_path
          prompt = prompt
          timeout = 120  (_CLAUDE_CLI_HARD_TIMEOUT_SECONDS)
        """
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result()

        scheduler = _make_scheduler(cli_dispatcher=mock_dispatcher)

        repo_path = "/some/repo"
        prompt = "Describe this repository."
        scheduler._invoke_claude_cli(repo_path=repo_path, prompt=prompt)

        mock_dispatcher.dispatch.assert_called_once()
        call_obj = mock_dispatcher.dispatch.call_args

        dispatched_flow = _get_dispatch_arg(call_obj, "flow", 0)
        assert dispatched_flow == _EXPECTED_FLOW

        dispatched_cwd = _get_dispatch_arg(call_obj, "cwd", 1)
        assert dispatched_cwd == repo_path

        dispatched_prompt = _get_dispatch_arg(call_obj, "prompt", 2)
        assert dispatched_prompt == prompt

        dispatched_timeout = _get_dispatch_arg(call_obj, "timeout", 3)
        assert dispatched_timeout == _EXPECTED_TIMEOUT


# ---------------------------------------------------------------------------
# Test 4a: success shape
# ---------------------------------------------------------------------------


class TestSchedulerResultShapeSuccess:
    """On success, _invoke_claude_cli returns (True, output_str)."""

    def test_success_result_returns_true_and_output(self) -> None:
        """
        When dispatcher.dispatch returns a successful InvocationResult,
        _invoke_claude_cli returns (True, result.output).
        """
        # Must exceed 100 chars to pass _validate_cli_output's minimum-length check.
        expected_output = (
            "---\ntitle: My Repo\n---\n"
            "This repository provides a comprehensive set of utilities for "
            "indexing and searching code at scale using semantic embeddings."
        )
        result = InvocationResult(
            success=True,
            output=expected_output,
            error="",
            cli_used="claude",
            was_failover=False,
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = result
        scheduler = _make_scheduler(cli_dispatcher=mock_dispatcher)

        success, output = scheduler._invoke_claude_cli("/repo", "prompt text here")

        assert success is True
        assert output == expected_output


# ---------------------------------------------------------------------------
# Test 4b: failure shape
# ---------------------------------------------------------------------------


class TestSchedulerResultShapeFailure:
    """On failure, _invoke_claude_cli returns (False, error_str)."""

    def test_failure_result_returns_false_and_error_string(self) -> None:
        """
        When dispatcher.dispatch returns a failed InvocationResult,
        _invoke_claude_cli returns (False, <error description>) — shape is
        identical to the pre-wiring (False, error_msg) tuple.
        """
        result = _make_failure_result()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = result
        scheduler = _make_scheduler(cli_dispatcher=mock_dispatcher)

        success, output = scheduler._invoke_claude_cli("/repo", "prompt text here")

        assert success is False
        assert isinstance(output, str) and output
        assert "CLI exploded" in output


# ---------------------------------------------------------------------------
# Test 5: failover logging
# ---------------------------------------------------------------------------


class TestSchedulerFailoverLogging:
    """When result.was_failover=True, an INFO log records cli_used and was_failover."""

    def test_failover_info_log_emitted(self, caplog) -> None:
        """
        When dispatcher returns was_failover=True, _invoke_claude_cli emits
        an INFO-level log entry containing:
          - "failover" (the concept)
          - "codex"    (the cli_used value)
          - "true"     (the was_failover=True state, case-insensitive)
        """
        result = _make_success_result(cli_used="codex", was_failover=True)
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = result
        scheduler = _make_scheduler(cli_dispatcher=mock_dispatcher)

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.description_refresh_scheduler",
        ):
            scheduler._invoke_claude_cli("/repo", "prompt text here")

        failover_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "failover" in r.message.lower()
        ]
        assert failover_records, (
            "Expected an INFO log containing 'failover' when was_failover=True, "
            f"but got records: {[r.message for r in caplog.records]}"
        )
        # Must mention the CLI that handled the job
        assert any("codex" in r.message.lower() for r in failover_records), (
            f"Expected 'codex' in failover log message, got: "
            f"{[r.message for r in failover_records]}"
        )
        # Must explicitly indicate the was_failover=True state
        assert any("true" in r.message.lower() for r in failover_records), (
            f"Expected 'true' (was_failover=True) in failover log message, got: "
            f"{[r.message for r in failover_records]}"
        )
