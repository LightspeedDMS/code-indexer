"""
Tests asserting the isolation controls DependencyMapAnalyzer._invoke_claude_cli
(the direct Pass-2-retry subprocess path, used only when the primary
CliDispatcher attempt hits max-turns exhaustion or produces insufficient
output) applies: a neutral working directory (never golden-repos root) and
an always-on, explicit MCP server list. Full built-in tool capability (Bash,
Write, Edit, git) and the original --dangerously-skip-permissions behavior
are unchanged from before these controls existed — Pass 2 relies on Write
for its file-based output and Bash for its activity journal.

All subprocess calls are mocked via unittest.mock.patch — no real CLI runs.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import dependency_map_analyzer as dma
from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer


@pytest.fixture(autouse=True)
def _isolate_verification_semaphore():
    """Same isolation as test_dependency_map_analyzer.py — _invoke_claude_cli
    acquires the process-wide verification semaphore singleton, whose
    capacity is fixed by whichever test calls it first in the process."""
    import threading

    with patch(
        "code_indexer.global_repos.dependency_map_analyzer._get_verification_semaphore",
        return_value=threading.Semaphore(10),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_mcp_credential_by_default():
    """Most tests here don't care about MCP wiring; patch the registration
    singleton away so they don't depend on state left by other tests in the
    same process. Tests that DO care override this explicitly."""
    with patch(
        "code_indexer.server.services.mcp_self_registration_service."
        "MCPSelfRegistrationService.get_instance",
        return_value=None,
    ):
        yield


def _make_analyzer(tmp_path) -> DependencyMapAnalyzer:
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


class TestInvokeClaudeCliNeutralCwd:
    @patch("subprocess.run")
    def test_subprocess_cwd_is_not_golden_repos_root(self, mock_subprocess, tmp_path):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        actual_cwd = mock_subprocess.call_args[1]["cwd"]
        assert actual_cwd != str(tmp_path)

    @patch("subprocess.run")
    def test_golden_repos_root_passed_via_add_dir(self, mock_subprocess, tmp_path):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        cmd = mock_subprocess.call_args[0][0]
        assert "--add-dir" in cmd
        assert str(tmp_path) in cmd


class TestInvokeClaudeCliFullCommandCapability:
    @patch("subprocess.run")
    def test_dangerously_skip_permissions_present_when_requested(
        self, mock_subprocess, tmp_path
    ):
        """Original behavior restored: the flag is emitted only when the
        caller explicitly asks for it (Bash/Write need it to run
        non-interactively)."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(
            prompt="Test prompt",
            timeout=600,
            max_turns=8,
            dangerously_skip_permissions=True,
        )
        cmd = mock_subprocess.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("subprocess.run")
    def test_dangerously_skip_permissions_absent_when_not_requested(
        self, mock_subprocess, tmp_path
    ):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        cmd = mock_subprocess.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    @patch("subprocess.run")
    def test_no_tool_restriction_flags_present(self, mock_subprocess, tmp_path):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        cmd = mock_subprocess.call_args[0][0]
        assert "--restricted" not in cmd
        assert "--tools" not in cmd
        assert "--setting-sources" in cmd
        assert cmd[cmd.index("--setting-sources") + 1] == "user"

    @patch("subprocess.run")
    def test_allowed_tools_value_still_passed_through_unchanged(
        self, mock_subprocess, tmp_path
    ):
        """The caller-specified MCP tool name is preserved verbatim in
        --allowedTools."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(
            prompt="Test prompt",
            timeout=600,
            max_turns=8,
            allowed_tools="mcp__cidx-local__search_code",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert "--allowedTools" in cmd
        assert cmd[cmd.index("--allowedTools") + 1] == "mcp__cidx-local__search_code"


class TestInvokeClaudeCliStrictMcpConfig:
    @patch("subprocess.run")
    def test_strict_mcp_config_always_present(self, mock_subprocess, tmp_path):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        cmd = mock_subprocess.call_args[0][0]
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd

    def test_mcp_config_file_present_when_credential_available(self, tmp_path):
        """The small-domain retry's --allowedTools mcp__cidx-local__search_code
        only reaches a real server when --mcp-config actually names it —
        --strict-mcp-config alone excludes the account's global registration."""
        fake_svc = MagicMock()
        fake_svc.get_cached_auth_header_value.return_value = "Basic abc123"
        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8123

        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=0, stdout="# Domain\n\nbody"
            )
            with patch(
                "code_indexer.server.services.mcp_self_registration_service."
                "MCPSelfRegistrationService.get_instance",
                return_value=fake_svc,
            ):
                with patch(
                    "code_indexer.server.services.agent_cli_isolation.get_config_service",
                    return_value=fake_config_service,
                ):
                    analyzer = _make_analyzer(tmp_path)
                    analyzer._invoke_claude_cli(
                        prompt="Test prompt",
                        timeout=600,
                        max_turns=8,
                        allowed_tools="mcp__cidx-local__search_code",
                    )
            cmd = mock_subprocess.call_args[0][0]
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" in cmd


class TestInvokeClaudeCliMcpConfigCleanupOnException:
    def test_mcp_config_file_removed_even_if_exception_before_subprocess(
        self, tmp_path
    ):
        """The MCP config file is built and written to disk before the
        subprocess-wrapping try block; an exception raised anywhere in that
        window must still trigger cleanup rather than leave the credential
        file behind."""
        fake_svc = MagicMock()
        fake_svc.get_cached_auth_header_value.return_value = "Basic abc123"
        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8123

        captured_path = {}
        real_try_build = dma.try_build_mcp_config_file

        def _capture_and_delegate():
            path = real_try_build()
            captured_path["path"] = path
            return path

        def _raise_after_mcp_config_built(target):
            raise RuntimeError("boom")

        with patch(
            "code_indexer.server.services.mcp_self_registration_service."
            "MCPSelfRegistrationService.get_instance",
            return_value=fake_svc,
        ):
            with patch(
                "code_indexer.server.services.agent_cli_isolation.get_config_service",
                return_value=fake_config_service,
            ):
                with patch.object(
                    dma, "try_build_mcp_config_file", side_effect=_capture_and_delegate
                ):
                    with patch.object(
                        dma,
                        "prepare_stable_neutral_cwd",
                        side_effect=_raise_after_mcp_config_built,
                    ):
                        analyzer = _make_analyzer(tmp_path)
                        with pytest.raises(RuntimeError, match="boom"):
                            analyzer._invoke_claude_cli(
                                prompt="Test prompt", timeout=600, max_turns=8
                            )

        built_path = captured_path.get("path")
        assert built_path is not None, (
            "the mcp config file must have been built for this test to be valid"
        )
        assert not os.path.exists(built_path), (
            "mcp config file must be cleaned up even when an exception occurs "
            "before the subprocess is started"
        )


class TestInvokeClaudeCliStableCwd:
    """Fleet-scale follow-up: a unique cwd per call means the claude CLI's
    own per-cwd session-transcript folder grows one new folder PER CALL
    instead of one per golden-repos root. The scratch dir must be the SAME
    directory across calls for the same golden_repos_root, and must survive
    (along with anything the agent wrote into it) after _invoke_claude_cli
    returns."""

    @patch("subprocess.run")
    def test_same_golden_repos_root_reuses_same_scratch_dir_across_calls(
        self, mock_subprocess, tmp_path
    ):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        analyzer = _make_analyzer(tmp_path)
        analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)
        first_cwd = mock_subprocess.call_args[1]["cwd"]
        analyzer._invoke_claude_cli(
            prompt="Test prompt again", timeout=600, max_turns=8
        )
        second_cwd = mock_subprocess.call_args[1]["cwd"]
        assert first_cwd == second_cwd

    @patch("subprocess.run")
    def test_scratch_dir_and_agent_written_file_persist_after_call(
        self, mock_subprocess, tmp_path
    ):
        """Once the agent has Write/Bash access, it may create files inside
        its own neutral scratch cwd; the directory (and anything the agent
        wrote there) must survive the call, since it is a stable directory
        reused on the NEXT call for the same golden_repos_root, not a
        fresh one-off cleaned up after every call."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="# Domain\n\nbody"
        )
        created_dirs = []
        real_prepare = dma.prepare_stable_neutral_cwd

        def _prepare_and_populate(target):
            created = real_prepare(target)
            created_dirs.append(created)
            with open(os.path.join(created, "agent_wrote_this.txt"), "w") as fh:
                fh.write("hello")
            return created

        with patch.object(
            dma, "prepare_stable_neutral_cwd", side_effect=_prepare_and_populate
        ):
            analyzer = _make_analyzer(tmp_path)
            analyzer._invoke_claude_cli(prompt="Test prompt", timeout=600, max_turns=8)

        assert created_dirs, "scratch dir should have been created"
        assert os.path.isdir(created_dirs[0]), (
            "scratch dir must persist after the call returns"
        )
        assert os.path.exists(os.path.join(created_dirs[0], "agent_wrote_this.txt")), (
            "a file the agent wrote into the scratch dir must persist too"
        )
