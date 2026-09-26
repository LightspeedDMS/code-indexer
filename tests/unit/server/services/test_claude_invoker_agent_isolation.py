"""
Tests asserting the isolation controls ClaudeInvoker applies when analyzing
a golden repository: a neutral working directory (so the repository's own
CLAUDE.md is not auto-loaded as trusted CLI configuration) and an explicit,
always-on MCP server list (so the account's other globally-registered MCP
servers are never pulled into the session). Every other command capability
— Bash, Write, Edit, git, --dangerously-skip-permissions — is unchanged from
before these controls existed; the analysis prompts rely on it.

self_monitoring_scan queries the server's own log database and keeps the
pre-existing invocation shape unchanged.

All subprocess calls are mocked via unittest.mock.patch — no real CLI runs.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from code_indexer.server.services.claude_invoker import ClaudeInvoker


def _make_invoker(analysis_model: str = "opus", soft_timeout_seconds: int = 90):
    return ClaudeInvoker(
        analysis_model=analysis_model, soft_timeout_seconds=soft_timeout_seconds
    )


def _completed_process(returncode: int = 0, stdout: str = "", stderr: str = ""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestClaudeInvokerNeutralCwd:
    def test_subprocess_cwd_is_not_the_analyzed_directory(self):
        """The analyzed directory is never the subprocess's own cwd, so the
        CLI does not auto-load that directory's own CLAUDE.md as trusted
        configuration; the directory stays fully reachable via --add-dir
        (read and write)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/example-repo",
                prompt="analyze",
                timeout=30,
            )
            _, kwargs = mock_run.call_args
            assert kwargs.get("cwd") != "/golden-repos/example-repo"

    def test_analyzed_directory_is_passed_via_add_dir(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/example-repo",
                prompt="analyze",
                timeout=30,
            )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--add-dir" in cmd_str
            assert "/golden-repos/example-repo" in cmd_str

    def test_dependency_map_pass1_neutral_cwd(self):
        """Same neutral-cwd treatment applies to the golden-repos-root-wide
        dependency-map flows, not just single-repo lifecycle analysis."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="dependency_map_pass_1",
                cwd="/golden-repos",
                prompt="analyze",
                timeout=30,
            )
            _, kwargs = mock_run.call_args
            assert kwargs.get("cwd") != "/golden-repos"


class TestClaudeInvokerStableCwd:
    """Fleet-scale follow-up: a unique cwd per call means the claude CLI's
    own per-cwd session-transcript folder grows one new folder PER CALL
    instead of one per repo. The subprocess cwd must be the SAME directory
    across calls for the same target, and must survive after invoke()
    returns (never deleted), restoring the pre-existing one-folder-per-repo
    transcript behaviour."""

    def test_same_target_reuses_the_same_subprocess_cwd_across_calls(self):
        import shutil

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/stable-cwd-repo",
                prompt="analyze",
                timeout=30,
            )
            first_cwd = mock_run.call_args[1].get("cwd")
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/stable-cwd-repo",
                prompt="analyze again",
                timeout=30,
            )
            second_cwd = mock_run.call_args[1].get("cwd")
        try:
            assert first_cwd == second_cwd
        finally:
            shutil.rmtree(first_cwd, ignore_errors=True)

    def test_subprocess_cwd_directory_persists_after_invoke_returns(self):
        import os
        import shutil

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/persistent-cwd-repo",
                prompt="analyze",
                timeout=30,
            )
            actual_cwd = mock_run.call_args[1].get("cwd")
        try:
            assert os.path.isdir(actual_cwd)
        finally:
            shutil.rmtree(actual_cwd, ignore_errors=True)


class TestClaudeInvokerFullCommandCapability:
    def test_dangerously_skip_permissions_present_for_lifecycle(self):
        """Bash/Write/Edit/git all need --dangerously-skip-permissions to
        run non-interactively; every non-exempt flow keeps it."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/repo-a",
                prompt="p",
                timeout=30,
            )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--dangerously-skip-permissions" in cmd_str

    def test_no_tool_restriction_flags_present(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/repo-a",
                prompt="p",
                timeout=30,
            )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--restricted" not in cmd_str
            assert "--tools" not in cmd_str

    def test_setting_sources_disabled(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="repo_lifecycle",
                cwd="/golden-repos/repo-a",
                prompt="p",
                timeout=30,
            )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--setting-sources" in cmd_str


class TestClaudeInvokerStrictMcpConfig:
    def test_strict_mcp_config_present_even_when_no_credential_available(self):
        """Even when the MCP self-registration singleton is unavailable, the
        invocation still runs with --strict-mcp-config alone (zero servers)
        rather than omitting the flag and inheriting whatever MCP servers
        are registered on the account."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            with patch(
                "code_indexer.server.services.mcp_self_registration_service."
                "MCPSelfRegistrationService.get_instance",
                return_value=None,
            ):
                invoker = _make_invoker()
                invoker.invoke(
                    flow="repo_lifecycle",
                    cwd="/golden-repos/repo-a",
                    prompt="p",
                    timeout=30,
                )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--strict-mcp-config" in cmd_str
            assert "--mcp-config" not in cmd_str

    def test_mcp_registration_available_adds_mcp_config_file(self):
        """When a header is obtainable, --strict-mcp-config plus a private
        per-invocation --mcp-config file naming ONLY cidx-local is emitted."""
        fake_svc = MagicMock()
        fake_svc.get_cached_auth_header_value.return_value = "Basic abc123"
        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8123

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            with patch(
                "code_indexer.server.services.mcp_self_registration_service."
                "MCPSelfRegistrationService.get_instance",
                return_value=fake_svc,
            ):
                with patch(
                    "code_indexer.server.services.agent_cli_isolation.get_config_service",
                    return_value=fake_config_service,
                ):
                    invoker = _make_invoker()
                    invoker.invoke(
                        flow="repo_lifecycle",
                        cwd="/golden-repos/repo-a",
                        prompt="p",
                        timeout=30,
                    )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--strict-mcp-config" in cmd_str
            assert "--mcp-config" in cmd_str


class TestClaudeInvokerSelfMonitoringExemption:
    def test_self_monitoring_scan_keeps_original_cwd(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )
            _, kwargs = mock_run.call_args
            assert kwargs.get("cwd") == "/opt/cidx-server"

    def test_self_monitoring_scan_keeps_dangerously_skip_permissions(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="out")
            invoker = _make_invoker()
            invoker.invoke(
                flow="self_monitoring_scan",
                cwd="/opt/cidx-server",
                prompt="p",
                timeout=30,
            )
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--dangerously-skip-permissions" in cmd_str
            assert "--strict-mcp-config" not in cmd_str
