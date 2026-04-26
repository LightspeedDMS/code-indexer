"""
Tests for CliDispatcher wiring into ClaudeCliManager (Story #847).

These tests verify that ClaudeCliManager._process_work routes through a
CliDispatcher instead of the legacy direct-Claude placeholder path.

Test inventory (3 tests):
    Test 1 — queue-worker invokes dispatcher.dispatch (mock dispatcher).
    Test 2 — codex_weight=0.0 path produces output identical to
              legacy direct-Claude behavior (no shape change for callers).
    Test 3 — dispatcher result.output is forwarded verbatim to the
              callback(success, result) call (no shape change).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_result(
    output: str = "generated description", cli_used: str = "claude"
) -> InvocationResult:
    return InvocationResult(
        success=True,
        output=output,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _make_manager(cli_dispatcher=None) -> "ClaudeCliManager":  # noqa: F821
    """Build a ClaudeCliManager with zero worker threads for deterministic testing."""
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

    kwargs: dict = dict(api_key=None, max_workers=0)
    if cli_dispatcher is not None:
        kwargs["cli_dispatcher"] = cli_dispatcher

    return ClaudeCliManager(**kwargs)


# ---------------------------------------------------------------------------
# Test 1: worker invokes dispatcher.dispatch
# ---------------------------------------------------------------------------


class TestManagerWorkerInvokesDispatcher:
    """_process_work must call dispatcher.dispatch, not run a direct CLI subprocess."""

    def test_process_work_calls_dispatcher_dispatch(self) -> None:
        """
        When _process_work is called with a repo_path and callback,
        the injected dispatcher.dispatch is invoked exactly once.
        """
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result(
            output="the answer"
        )

        manager = _make_manager(cli_dispatcher=mock_dispatcher)

        received: list = []

        def callback(success, result):
            received.append((success, result))

        repo_path = Path("/fake/repo")
        manager._process_work(repo_path, callback)

        mock_dispatcher.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: codex_weight=0.0 is equivalent to legacy Claude-only
# ---------------------------------------------------------------------------


class TestManagerCodexWeightZeroEquivalentToLegacy:
    """With codex_weight=0.0, dispatcher always selects Claude — no behavioral change."""

    def test_codex_weight_zero_always_uses_claude(self) -> None:
        """
        When the injected dispatcher is built with codex=None (codex_weight=0.0),
        dispatch always returns a Claude result. The callback receives the same
        (success, output) tuple it would have received from the legacy path.
        """
        from code_indexer.server.services.cli_dispatcher import CliDispatcher
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        mock_claude = MagicMock(spec=ClaudeInvoker)
        expected_output = "legacy-equivalent output"
        mock_claude.invoke.return_value = _make_success_result(
            output=expected_output, cli_used="claude"
        )

        # codex=None, codex_weight=0.0 → all dispatches go to Claude
        dispatcher = CliDispatcher(claude=mock_claude, codex=None, codex_weight=0.0)
        manager = _make_manager(cli_dispatcher=dispatcher)

        received: list = []

        def callback(success, result):
            received.append((success, result))

        manager._process_work(Path("/repo"), callback)

        assert len(received) == 1
        success, output = received[0]
        assert success is True
        assert output == expected_output
        # Claude was called; no Codex involved
        mock_claude.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: dispatcher result.output forwarded verbatim to callback
# ---------------------------------------------------------------------------


class TestManagerCallbackReceivesDispatcherOutput:
    """The callback must receive exactly result.output from the dispatcher."""

    def test_callback_receives_exact_dispatcher_output(self) -> None:
        """
        When dispatcher.dispatch returns InvocationResult.output,
        _process_work passes that exact string to callback(success=True, result=output).
        No intermediate transformation may occur.
        """
        expected_output = "verbatim dispatcher output string"
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result(
            output=expected_output
        )

        manager = _make_manager(cli_dispatcher=mock_dispatcher)

        received: list = []

        def callback(success, result):
            received.append((success, result))

        manager._process_work(Path("/repo"), callback)

        assert len(received) == 1
        success, output = received[0]
        assert success is True
        assert output == expected_output
