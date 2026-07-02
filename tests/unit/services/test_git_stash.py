"""
Unit tests for git stash operations in GitOperationsService.

Story #453: git_stash - Stash and restore uncommitted changes

Tests:
  - git_stash_push: runs 'git stash push' with optional message
  - git_stash_push: returns success, stash_ref, message
  - git_stash_pop: runs 'git stash pop stash@{N}'
  - git_stash_pop: returns success, message
  - git_stash_apply: runs 'git stash apply stash@{N}'
  - git_stash_apply: returns success, message
  - git_stash_list: runs 'git stash list' and parses output
  - git_stash_list: returns list of stash entries
  - git_stash_drop: runs 'git stash drop stash@{N}'
  - git_stash_drop: returns success, message
  - Error handling: GitCommandError raised on git failures
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


class TestGitStashPush:
    """Tests for git_stash_push method."""

    def test_git_stash_push_without_message_runs_stash_push(self, git_ops_service):
        """git_stash_push without message runs 'git stash push'."""
        mock_result = Mock(
            returncode=0,
            stdout="Saved working directory and index state WIP on main: abc123 commit",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_push(Path("/tmp/repo"))

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "stash" in cmd
        assert "push" in cmd
        assert "-m" not in cmd

    def test_git_stash_push_with_message_includes_dash_m(self, git_ops_service):
        """git_stash_push with message includes -m flag."""
        mock_result = Mock(
            returncode=0,
            stdout="Saved working directory and index state On main: my stash",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_push(Path("/tmp/repo"), message="my stash")

        cmd = mock_run.call_args[0][0]
        assert "-m" in cmd
        assert "my stash" in cmd

    def test_git_stash_push_returns_success_true(self, git_ops_service):
        """git_stash_push returns success=True on success."""
        mock_result = Mock(
            returncode=0,
            stdout="Saved working directory and index state WIP on main: abc123",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_push(Path("/tmp/repo"))

        assert result["success"] is True

    def test_git_stash_push_returns_stash_ref(self, git_ops_service):
        """git_stash_push returns stash_ref='stash@{0}'."""
        mock_result = Mock(
            returncode=0,
            stdout="Saved working directory and index state WIP on main: abc123",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_push(Path("/tmp/repo"))

        assert result["stash_ref"] == "stash@{0}"

    def test_git_stash_push_returns_message(self, git_ops_service):
        """git_stash_push returns message from git output."""
        stash_output = (
            "Saved working directory and index state WIP on main: abc123 initial commit"
        )
        mock_result = Mock(returncode=0, stdout=stash_output, stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_push(Path("/tmp/repo"))

        assert result["message"] == stash_output

    def test_git_stash_push_raises_git_command_error_on_failure(self, git_ops_service):
        """git_stash_push raises GitCommandError on subprocess failure."""
        import subprocess
        from code_indexer.server.services.git_operations_service import GitCommandError

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(
                1, "git stash push", stderr="nothing to stash"
            ),
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_stash_push(Path("/tmp/repo"))


class TestGitStashPop:
    """Tests for git_stash_pop method."""

    def test_git_stash_pop_runs_stash_pop_with_index(self, git_ops_service):
        """git_stash_pop runs 'git stash pop stash@{N}'."""
        mock_result = Mock(
            returncode=0,
            stdout="On branch main\nChanges not staged for commit:",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_pop(Path("/tmp/repo"), index=0)

        cmd = mock_run.call_args[0][0]
        assert "stash" in cmd
        assert "pop" in cmd
        assert "stash@{0}" in cmd

    def test_git_stash_pop_with_nonzero_index(self, git_ops_service):
        """git_stash_pop with index=2 uses 'stash@{2}'."""
        mock_result = Mock(returncode=0, stdout="Applied stash", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_pop(Path("/tmp/repo"), index=2)

        cmd = mock_run.call_args[0][0]
        assert "stash@{2}" in cmd

    def test_git_stash_pop_returns_success_true(self, git_ops_service):
        """git_stash_pop returns success=True on success."""
        mock_result = Mock(returncode=0, stdout="Applied stash@{0}", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_pop(Path("/tmp/repo"))

        assert result["success"] is True

    def test_git_stash_pop_raises_git_command_error_on_failure(self, git_ops_service):
        """git_stash_pop raises GitCommandError on subprocess failure."""
        import subprocess
        from code_indexer.server.services.git_operations_service import GitCommandError

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(
                1, "git stash pop", stderr="error: No stash entries found"
            ),
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_stash_pop(Path("/tmp/repo"))


class TestGitStashApply:
    """Tests for git_stash_apply method."""

    def test_git_stash_apply_runs_stash_apply_with_index(self, git_ops_service):
        """git_stash_apply runs 'git stash apply stash@{N}'."""
        mock_result = Mock(
            returncode=0,
            stdout="On branch main\nChanges not staged for commit:",
            stderr="",
        )

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_apply(Path("/tmp/repo"), index=0)

        cmd = mock_run.call_args[0][0]
        assert "stash" in cmd
        assert "apply" in cmd
        assert "stash@{0}" in cmd

    def test_git_stash_apply_returns_success_true(self, git_ops_service):
        """git_stash_apply returns success=True on success."""
        mock_result = Mock(returncode=0, stdout="Applied stash@{1}", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_apply(Path("/tmp/repo"), index=1)

        assert result["success"] is True

    def test_git_stash_apply_raises_git_command_error_on_failure(self, git_ops_service):
        """git_stash_apply raises GitCommandError on conflict/failure."""
        import subprocess
        from code_indexer.server.services.git_operations_service import GitCommandError

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(
                1, "git stash apply", stderr="error: Merge conflict in file.txt"
            ),
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_stash_apply(Path("/tmp/repo"))


class TestGitStashList:
    """Tests for git_stash_list method."""

    def test_git_stash_list_returns_success_true(self, git_ops_service):
        """git_stash_list returns success=True."""
        stash_output = "stash@{0}|||WIP on main: abc123 commit|||2024-01-15 10:30:00 +0000\nstash@{1}|||On main: my stash|||2024-01-14 09:00:00 +0000"
        mock_result = Mock(returncode=0, stdout=stash_output, stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_list(Path("/tmp/repo"))

        assert result["success"] is True

    def test_git_stash_list_returns_stashes_list(self, git_ops_service):
        """git_stash_list returns list of stash entries."""
        stash_output = "stash@{0}|||WIP on main: abc123 commit|||2024-01-15 10:30:00 +0000\nstash@{1}|||On main: my stash|||2024-01-14 09:00:00 +0000"
        mock_result = Mock(returncode=0, stdout=stash_output, stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_list(Path("/tmp/repo"))

        assert len(result["stashes"]) == 2
        assert result["stashes"][0]["index"] == 0
        assert result["stashes"][1]["index"] == 1

    def test_git_stash_list_parses_message_from_output(self, git_ops_service):
        """git_stash_list extracts message for each stash entry."""
        stash_output = (
            "stash@{0}|||WIP on main: abc123 my message|||2024-01-15 10:30:00 +0000"
        )
        mock_result = Mock(returncode=0, stdout=stash_output, stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_list(Path("/tmp/repo"))

        assert "WIP on main: abc123 my message" in result["stashes"][0]["message"]

    def test_git_stash_list_empty_returns_empty_list(self, git_ops_service):
        """git_stash_list returns empty list when no stashes."""
        mock_result = Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_list(Path("/tmp/repo"))

        assert result["success"] is True
        assert result["stashes"] == []

    def test_git_stash_list_uses_format_with_separator(self, git_ops_service):
        """git_stash_list uses format with ||| separator for parsing."""
        mock_result = Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_list(Path("/tmp/repo"))

        cmd = mock_run.call_args[0][0]
        assert "stash" in cmd
        assert "list" in cmd


class TestGitStashDrop:
    """Tests for git_stash_drop method."""

    def test_git_stash_drop_runs_stash_drop_with_index(self, git_ops_service):
        """git_stash_drop runs 'git stash drop stash@{N}'."""
        mock_result = Mock(returncode=0, stdout="Dropped stash@{0} (abc123)", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ) as mock_run:
            git_ops_service.git_stash_drop(Path("/tmp/repo"), index=0)

        cmd = mock_run.call_args[0][0]
        assert "stash" in cmd
        assert "drop" in cmd
        assert "stash@{0}" in cmd

    def test_git_stash_drop_returns_success_true(self, git_ops_service):
        """git_stash_drop returns success=True on success."""
        mock_result = Mock(returncode=0, stdout="Dropped stash@{0} (abc123)", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_drop(Path("/tmp/repo"))

        assert result["success"] is True

    def test_git_stash_drop_returns_message(self, git_ops_service):
        """git_stash_drop returns message from git output."""
        drop_output = "Dropped stash@{0} (abc123def456)"
        mock_result = Mock(returncode=0, stdout=drop_output, stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=mock_result,
        ):
            result = git_ops_service.git_stash_drop(Path("/tmp/repo"))

        assert result["message"] == drop_output

    def test_git_stash_drop_raises_git_command_error_on_failure(self, git_ops_service):
        """git_stash_drop raises GitCommandError when stash not found."""
        import subprocess
        from code_indexer.server.services.git_operations_service import GitCommandError

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(
                1, "git stash drop", stderr="error: refs/stash: does not exist"
            ),
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_stash_drop(Path("/tmp/repo"), index=5)
