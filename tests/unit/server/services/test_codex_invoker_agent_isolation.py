"""
Tests asserting the isolation control CodexInvoker applies when analyzing a
golden repository: a neutral working directory, so the repository's own
AGENTS.md is not auto-loaded as trusted CLI configuration. Codex keeps its
full command capability (--dangerously-bypass-approvals-and-sandbox is
unconditional) — that flag is unchanged from before this control existed,
including for self_monitoring_scan: that flow is routed to Claude only by
CliDispatcher, so CodexInvoker itself carries no flow-specific behaviour.

All subprocess calls are mocked via unittest.mock.patch — no real CLI runs.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_indexer.server.services.codex_invoker import CodexInvoker

_FAKE_CODEX_HOME = "/fake/codex-home"


def _make_invoker(codex_home: str = _FAKE_CODEX_HOME) -> CodexInvoker:
    return CodexInvoker(codex_home=codex_home)


def _agent_message_event(text: str) -> dict:
    return {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }


def _make_success_proc(text: str = "output text") -> MagicMock:
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = 0
    proc.communicate.return_value = (json.dumps(_agent_message_event(text)), "")
    return proc


def _invoke_and_capture_popen(invoker, *, flow: str, cwd: str = "/golden-repos/repo-a"):
    proc = _make_success_proc()
    with patch("subprocess.Popen", return_value=proc) as mock_popen:
        invoker.invoke(flow=flow, cwd=cwd, prompt="analyze", timeout=60)
    return mock_popen.call_args[0][0], mock_popen.call_args[1]


class TestCodexInvokerNeutralCwd:
    def test_subprocess_cwd_is_not_the_analyzed_directory(self):
        _, kwargs = _invoke_and_capture_popen(
            _make_invoker(), flow="repo_lifecycle", cwd="/golden-repos/example-repo"
        )
        actual_cwd = kwargs.get("cwd")
        assert actual_cwd != "/golden-repos/example-repo"

    def test_dependency_map_pass1_neutral_cwd(self):
        _, kwargs = _invoke_and_capture_popen(
            _make_invoker(), flow="dependency_map_pass_1", cwd="/golden-repos"
        )
        actual_cwd = kwargs.get("cwd")
        assert actual_cwd != "/golden-repos", "cwd must not equal golden-repos root"


class TestCodexInvokerStableCwd:
    """Fleet-scale follow-up: a unique cwd per call would grow codex's own
    per-cwd session/rollout storage one entry PER CALL instead of one per
    repo. The subprocess cwd must be the SAME directory across calls for the
    same target, and must survive after invoke() returns."""

    def test_same_target_reuses_the_same_subprocess_cwd_across_calls(self):
        import shutil

        invoker = _make_invoker()
        _, first_kwargs = _invoke_and_capture_popen(
            invoker, flow="repo_lifecycle", cwd="/golden-repos/stable-cwd-repo"
        )
        _, second_kwargs = _invoke_and_capture_popen(
            invoker, flow="repo_lifecycle", cwd="/golden-repos/stable-cwd-repo"
        )
        first_cwd = first_kwargs.get("cwd")
        second_cwd = second_kwargs.get("cwd")
        try:
            assert first_cwd == second_cwd
        finally:
            shutil.rmtree(first_cwd, ignore_errors=True)

    def test_subprocess_cwd_directory_persists_after_invoke_returns(self):
        import os
        import shutil

        invoker = _make_invoker()
        _, kwargs = _invoke_and_capture_popen(
            invoker, flow="repo_lifecycle", cwd="/golden-repos/persistent-cwd-repo"
        )
        actual_cwd = kwargs.get("cwd")
        try:
            assert os.path.isdir(actual_cwd)
        finally:
            shutil.rmtree(actual_cwd, ignore_errors=True)


class TestCodexInvokerFullCommandCapability:
    def test_dangerously_bypass_flag_always_present(self):
        """Codex keeps its original command capability — the bypass flag is
        unconditional; the neutral cwd is the only isolation change."""
        cmd, _ = _invoke_and_capture_popen(_make_invoker(), flow="repo_lifecycle")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_command_shape_unchanged_for_dependency_map(self):
        cmd, _ = _invoke_and_capture_popen(
            _make_invoker(), flow="dependency_map_verification"
        )
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "--json" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd


class TestCodexInvokerSelfMonitoringScanUnaffected:
    """self_monitoring_scan is routed to Claude only by CliDispatcher (never
    reaches CodexInvoker.invoke() through any supported code path). Codex
    itself carries no special case for it: the bypass flag and command
    shape are exactly the same as every other flow."""

    def test_self_monitoring_scan_keeps_original_cwd(self):
        result = _invoke_and_capture_popen(
            _make_invoker(), flow="self_monitoring_scan", cwd="/opt/cidx-server"
        )
        cwd_value = result[1].get("cwd")
        assert cwd_value == "/opt/cidx-server"

    def test_self_monitoring_scan_still_gets_bypass_flag(self):
        cmd, _ = _invoke_and_capture_popen(
            _make_invoker(), flow="self_monitoring_scan", cwd="/opt/cidx-server"
        )
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--sandbox" not in cmd

    def test_other_flows_still_get_bypass_flag_unaffected(self):
        cmd, _ = _invoke_and_capture_popen(_make_invoker(), flow="repo_lifecycle")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--sandbox" not in cmd
