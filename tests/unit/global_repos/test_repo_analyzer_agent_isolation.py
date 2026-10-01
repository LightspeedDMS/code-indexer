"""
Tests asserting the isolation controls invoke_claude_cli (repo_analyzer.py)
applies when analyzing a golden repository: a neutral working directory
(never the analyzed repository) and an always-on, explicit MCP server list.
Full built-in tool capability (Bash, Write, Edit, git) and
--dangerously-skip-permissions are unchanged from before these controls
existed — the description-generation prompt relies on being able to explore
freely.

All subprocess calls are mocked via unittest.mock.patch — no real CLI runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.global_repos.repo_analyzer import invoke_claude_cli


def _make_proc(returncode: int = 0, stdout: str = "ok", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _no_mcp_credential():
    """Context manager patching the MCP registration singleton away, so
    these tests don't depend on whatever state other tests left behind."""
    return patch(
        "code_indexer.server.services.mcp_self_registration_service."
        "MCPSelfRegistrationService.get_instance",
        return_value=None,
    )


class TestInvokeClaudeCliNeutralCwd:
    def test_subprocess_cwd_is_not_the_analyzed_directory(self, tmp_path):
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        actual_cwd = mock_popen.call_args[1]["cwd"]
        assert actual_cwd != str(tmp_path)

    def test_analyzed_directory_passed_via_add_dir(self, tmp_path):
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        cmd_str = " ".join(mock_popen.call_args[0][0])
        assert "--add-dir" in cmd_str
        assert str(tmp_path) in cmd_str


class TestInvokeClaudeCliStableCwd:
    """Fleet-scale follow-up: a unique cwd per call means the claude CLI's
    own per-cwd session-transcript folder grows one new folder PER CALL
    instead of one per repo. The subprocess cwd must be the SAME directory
    across calls for the same repo_path, and must survive after
    invoke_claude_cli() returns."""

    def test_same_repo_path_reuses_the_same_subprocess_cwd_across_calls(self):
        import shutil

        repo_path = "/golden-repos/stable-cwd-repo"
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(repo_path, "analyze this repo", 90, 120)
                first_cwd = mock_popen.call_args[1]["cwd"]
                invoke_claude_cli(repo_path, "analyze again", 90, 120)
                second_cwd = mock_popen.call_args[1]["cwd"]
        try:
            assert first_cwd == second_cwd
        finally:
            shutil.rmtree(first_cwd, ignore_errors=True)

    def test_subprocess_cwd_directory_persists_after_call_returns(self):
        import os
        import shutil

        repo_path = "/golden-repos/persistent-cwd-repo"
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(repo_path, "analyze this repo", 90, 120)
                actual_cwd = mock_popen.call_args[1]["cwd"]
        try:
            assert os.path.isdir(actual_cwd)
        finally:
            shutil.rmtree(actual_cwd, ignore_errors=True)


class TestInvokeClaudeCliFullCommandCapability:
    def test_dangerously_skip_permissions_present(self, tmp_path):
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        cmd_str = " ".join(mock_popen.call_args[0][0])
        assert "--dangerously-skip-permissions" in cmd_str

    def test_no_tool_restriction_flags_present(self, tmp_path):
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        cmd_str = " ".join(mock_popen.call_args[0][0])
        assert "--restricted" not in cmd_str
        assert "--tools" not in cmd_str

    def test_setting_sources_disabled(self, tmp_path):
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        cmd_str = " ".join(mock_popen.call_args[0][0])
        assert "--setting-sources" in cmd_str


class TestInvokeClaudeCliStrictMcpConfig:
    def test_strict_mcp_config_always_present(self, tmp_path):
        """Even though this prompt never calls cidx-local, --strict-mcp-config
        is unconditional so the account's other MCP servers never leak in."""
        with _no_mcp_credential():
            with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
                invoke_claude_cli(str(tmp_path), "analyze this repo", 90, 120)
        cmd_str = " ".join(mock_popen.call_args[0][0])
        assert "--strict-mcp-config" in cmd_str
