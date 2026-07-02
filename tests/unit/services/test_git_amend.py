"""
Unit tests for git_amend operation in GitOperationsService.

Story #454: git_amend - Amend the most recent git commit

Tests:
  - git_amend with message: runs 'git commit --amend -m "msg"'
  - git_amend without message: runs 'git commit --amend --no-edit'
  - git_amend returns success=True, commit_hash, message
  - git_amend sets GIT_AUTHOR/COMMITTER env vars when env provided
  - git_amend raises GitCommandError on subprocess failure
  - git_amend parses new commit hash from rev-parse HEAD
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def git_ops_service():
    """Create GitOperationsService bypassing __init__."""
    from code_indexer.server.services.git_operations_service import GitOperationsService

    service = GitOperationsService.__new__(GitOperationsService)
    timeouts = MagicMock()
    timeouts.git_local_timeout = 30
    service._git_timeouts = timeouts
    return service


class TestGitAmend:
    """Tests for git_amend method (Story #454)."""

    def _mock_run(
        self,
        commit_hash="newcommithash123",
        amend_stdout="[main abc1234] Amended commit",
    ):
        """Build a side_effect function for run_git_command in amend flow."""

        def side_effect(cmd, **kwargs):
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout=commit_hash, stderr="")
            if "commit" in cmd and "--amend" in cmd:
                return Mock(returncode=0, stdout=amend_stdout, stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        return side_effect

    def test_git_amend_with_message_runs_amend_dash_m(self, git_ops_service):
        """git_amend with message runs 'git commit --amend -m <msg>'."""
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(),
        ) as mock_run:
            git_ops_service.git_amend(Path("/tmp/repo"), message="New commit message")

        # Find the amend call
        amend_call = None
        for c in mock_run.call_args_list:
            if "--amend" in c[0][0]:
                amend_call = c
                break

        assert amend_call is not None
        cmd = amend_call[0][0]
        assert "--amend" in cmd
        assert "-m" in cmd
        assert "New commit message" in cmd
        assert "--no-edit" not in cmd

    def test_git_amend_without_message_runs_amend_no_edit(self, git_ops_service):
        """git_amend without message runs 'git commit --amend --no-edit'."""
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(),
        ) as mock_run:
            git_ops_service.git_amend(Path("/tmp/repo"))

        # Find the amend call
        amend_call = None
        for c in mock_run.call_args_list:
            if "--amend" in c[0][0]:
                amend_call = c
                break

        assert amend_call is not None
        cmd = amend_call[0][0]
        assert "--amend" in cmd
        assert "--no-edit" in cmd
        assert "-m" not in cmd

    def test_git_amend_returns_success_true(self, git_ops_service):
        """git_amend returns success=True on success."""
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(commit_hash="abc123def456"),
        ):
            result = git_ops_service.git_amend(Path("/tmp/repo"), message="Fix typo")

        assert result["success"] is True

    def test_git_amend_returns_commit_hash(self, git_ops_service):
        """git_amend returns the new commit hash from rev-parse HEAD."""
        expected_hash = "deadbeefcafe1234567890"

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(commit_hash=expected_hash),
        ):
            result = git_ops_service.git_amend(Path("/tmp/repo"), message="Fix")

        assert result["commit_hash"] == expected_hash

    def test_git_amend_returns_message(self, git_ops_service):
        """git_amend returns message field describing what was done."""
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(),
        ):
            result = git_ops_service.git_amend(Path("/tmp/repo"))

        assert "message" in result
        assert result["message"]

    def test_git_amend_with_env_passes_env_to_git_command(self, git_ops_service):
        """git_amend passes env dict to run_git_command."""
        custom_env = {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.com",
        }

        captured_envs = []

        def mock_run(cmd, **kwargs):
            if "--amend" in cmd:
                captured_envs.append(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] Amended", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc123", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            git_ops_service.git_amend(Path("/tmp/repo"), message="Fix", env=custom_env)

        assert len(captured_envs) == 1
        passed_env = captured_envs[0]
        assert passed_env.get("GIT_AUTHOR_NAME") == "Test Author"
        assert passed_env.get("GIT_AUTHOR_EMAIL") == "author@example.com"

    def test_git_amend_without_env_does_not_fail(self, git_ops_service):
        """git_amend works correctly when no env is provided."""
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=self._mock_run(),
        ):
            result = git_ops_service.git_amend(Path("/tmp/repo"), message="Fix")

        assert result["success"] is True

    def test_git_amend_raises_git_command_error_on_failure(self, git_ops_service):
        """git_amend raises GitCommandError when git command fails."""
        import subprocess
        from code_indexer.server.services.git_operations_service import GitCommandError

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(
                1, "git commit --amend", stderr="nothing to amend"
            ),
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_amend(Path("/tmp/repo"))

    def test_git_amend_calls_rev_parse_to_get_new_hash(self, git_ops_service):
        """git_amend calls 'git rev-parse HEAD' to get the new commit hash."""
        rev_parse_calls = []

        def mock_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                rev_parse_calls.append(cmd)
                return Mock(returncode=0, stdout="newhash123", stderr="")
            if "--amend" in cmd:
                return Mock(returncode=0, stdout="[main abc] Amended", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            git_ops_service.git_amend(Path("/tmp/repo"))

        assert len(rev_parse_calls) >= 1
        assert "HEAD" in rev_parse_calls[0]
