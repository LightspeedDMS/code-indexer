"""
Tests for CliDispatcher's handling of flow == "self_monitoring_scan":
  1. it is routed to Claude only, never Codex, regardless of codex_weight;
  2. it fails closed (never invoked at all) when the primary invoker is not
     the known, restricted ClaudeInvoker (e.g. a deployment-supplied
     CIDX_CLI_INVOKER plugin -- see cli_invoker_plugin.py).

The self-monitoring scan's safety model lives entirely inside the concrete
ClaudeInvoker class (its restricted --allowedTools/--permission-mode
policy in _build_claude_command), not in the generic IntelligenceCliInvoker
protocol a plugin implements. No plugin ships in this repo, and none is
installed by scripts/install-cidx-server.sh or the auto-updater today, but
the mechanism is real and reachable via an env var.

Every other flow must be completely unaffected by either behaviour: the
Claude/Codex weighted split still applies to them exactly as before, and
the invoker-identity check only fires for self_monitoring_scan.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from code_indexer.server.services.cli_dispatcher import CliDispatcher
from code_indexer.server.services.claude_invoker import ClaudeInvoker
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


class _FakePluginInvoker:
    """Stands in for an arbitrary CIDX_CLI_INVOKER plugin: implements the
    IntelligenceCliInvoker protocol shape but is NOT a ClaudeInvoker."""

    def __init__(self):
        self.calls = []

    def invoke(self, flow, cwd, prompt, timeout, max_turns=0):
        self.calls.append(flow)
        return InvocationResult(
            success=True,
            output="plugin output",
            error="",
            cli_used="plugin",
            was_failover=False,
        )


class _FakeCodexInvoker:
    """A codex-shaped stand-in used only to prove it is never called for
    self_monitoring_scan; every other flow may reach it normally."""

    def __init__(self):
        self.calls = []

    def invoke(self, flow, cwd, prompt, timeout, max_turns=0):
        self.calls.append(flow)
        return InvocationResult(
            success=True,
            output="codex output",
            error="",
            cli_used="codex",
            was_failover=False,
        )


def _make_real_claude_invoker() -> ClaudeInvoker:
    return ClaudeInvoker(analysis_model="opus", log_db_path="/opt/cidx-server/logs.db")


class TestSelfMonitoringScanRefusesNonStandardInvoker:
    def test_plugin_invoker_never_called_for_self_monitoring_scan(self):
        plugin = _FakePluginInvoker()
        dispatcher = CliDispatcher(claude=plugin, codex=None, codex_weight=0.0)

        dispatcher.dispatch(
            flow="self_monitoring_scan",
            cwd="/opt/cidx-server",
            prompt="p",
            timeout=30,
        )

        assert plugin.calls == [], (
            "a non-ClaudeInvoker plugin must never be invoked for self_monitoring_scan"
        )

    def test_plugin_invoker_returns_failure_result_for_self_monitoring_scan(self):
        plugin = _FakePluginInvoker()
        dispatcher = CliDispatcher(claude=plugin, codex=None, codex_weight=0.0)

        result = dispatcher.dispatch(
            flow="self_monitoring_scan",
            cwd="/opt/cidx-server",
            prompt="p",
            timeout=30,
        )

        assert result.success is False

    def test_other_flows_still_reach_the_plugin_invoker(self):
        """Regression: the guard is scoped to self_monitoring_scan only --
        every other flow keeps working through a plugin invoker exactly as
        before."""
        plugin = _FakePluginInvoker()
        dispatcher = CliDispatcher(claude=plugin, codex=None, codex_weight=0.0)

        result = dispatcher.dispatch(
            flow="repo_lifecycle",
            cwd="/golden-repos/repo-a",
            prompt="p",
            timeout=30,
        )

        assert plugin.calls == ["repo_lifecycle"]
        assert result.success is True
        assert result.output == "plugin output"

    def test_real_claude_invoker_is_not_blocked(self):
        """Regression: the guard never fires against the real ClaudeInvoker
        -- only against something that is NOT an instance of it."""
        real_invoker = _make_real_claude_invoker()
        dispatcher = CliDispatcher(claude=real_invoker, codex=None, codex_weight=0.0)

        # Patch subprocess.run so this stays a unit test with no real CLI call.
        import subprocess
        from unittest.mock import patch

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"status": "SUCCESS"}'
        mock_proc.stderr = ""
        with patch.object(subprocess, "run", return_value=mock_proc) as mock_run:
            result = dispatcher.dispatch(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )

        assert mock_run.called, "the real ClaudeInvoker must still be invoked"
        assert result.success is True


class TestSelfMonitoringScanRoutesToClaudeOnly:
    """Owner decision: self_monitoring_scan runs on Claude only. Codex must
    never receive this flow, whatever codex_weight is set to."""

    def test_codex_weight_one_still_routes_self_monitoring_scan_to_claude(self):
        """With codex_weight=1.0, every OTHER flow would deterministically
        select Codex as primary -- self_monitoring_scan must not."""
        codex = _FakeCodexInvoker()
        real_claude = _make_real_claude_invoker()
        dispatcher = CliDispatcher(claude=real_claude, codex=codex, codex_weight=1.0)

        import subprocess
        from unittest.mock import patch

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"status": "SUCCESS"}'
        mock_proc.stderr = ""
        with patch.object(subprocess, "run", return_value=mock_proc):
            dispatcher.dispatch(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )

        assert codex.calls == [], (
            "Codex must never receive self_monitoring_scan, even at codex_weight=1.0"
        )

    def test_codex_weight_one_still_routes_other_flows_to_codex(self):
        """Regression: the Claude-only routing is scoped to
        self_monitoring_scan -- every other flow's weighted split is
        unchanged. codex_weight=1.0 deterministically selects Codex."""
        real_claude = _make_real_claude_invoker()
        codex = _FakeCodexInvoker()
        dispatcher = CliDispatcher(claude=real_claude, codex=codex, codex_weight=1.0)

        result = dispatcher.dispatch(
            flow="repo_lifecycle",
            cwd="/golden-repos/repo-a",
            prompt="p",
            timeout=30,
        )

        assert codex.calls == ["repo_lifecycle"]
        assert result.cli_used == "codex"

    def test_self_monitoring_scan_never_fails_over_to_codex_on_claude_failure(self):
        """Even when the real ClaudeInvoker fails outright (not just a
        retryable-on-same case), self_monitoring_scan must not fail over
        to Codex -- unlike every other flow's normal failover policy."""
        codex = _FakeCodexInvoker()
        real_claude = _make_real_claude_invoker()
        dispatcher = CliDispatcher(claude=real_claude, codex=codex, codex_weight=1.0)

        import subprocess
        from unittest.mock import patch

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "boom"
        with patch.object(subprocess, "run", return_value=mock_proc):
            result = dispatcher.dispatch(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )

        assert codex.calls == [], "Codex must never be used as a failover for this flow"
        assert result.success is False
        assert result.cli_used == "claude"

    def test_retry_bound_is_exactly_two_attempts_on_retryable_on_same(self):
        """Pin the Claude-only branch's retry bound: exactly 2 attempts when
        every attempt fails with RETRYABLE_ON_SAME (subprocess.TimeoutExpired
        maps to it in claude_invoker.py). A range(3) mutant (3 attempts)
        must fail this test."""
        real_claude = _make_real_claude_invoker()
        dispatcher = CliDispatcher(claude=real_claude, codex=None, codex_weight=0.0)

        import subprocess
        from unittest.mock import patch

        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
        ) as mock_run:
            result = dispatcher.dispatch(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )

        assert mock_run.call_count == 2, (
            f"expected exactly 2 Claude attempts, got {mock_run.call_count}"
        )
        assert result.success is False

    def test_plugin_invoker_still_fails_closed_under_claude_only_routing(self):
        """Regression: combining both behaviours -- Claude-only routing does
        not relax the invoker-identity guard. A plugin standing in for
        claude is still refused, and codex (even at weight 1.0) is still
        never touched."""
        plugin = _FakePluginInvoker()
        codex = _FakeCodexInvoker()
        dispatcher = CliDispatcher(claude=plugin, codex=codex, codex_weight=1.0)

        result = dispatcher.dispatch(
            flow="self_monitoring_scan",
            cwd="/opt/cidx-server",
            prompt="p",
            timeout=30,
        )

        assert plugin.calls == []
        assert codex.calls == []
        assert result.success is False
