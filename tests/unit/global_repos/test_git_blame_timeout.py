"""Unit tests for git blame timeout handling in GitOperationsService.

Bug #1008: git_blame MCP tool times out on repos with deep history.

Tests that get_blame() handles subprocess.TimeoutExpired gracefully
by returning an error dict instead of propagating the exception.
"""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

BLAME_TIMEOUT_SECONDS = 30
_PATCH_TARGET = "code_indexer.global_repos.git_operations.run_git_command"


@pytest.fixture
def git_service(tmp_path):
    """Return a GitOperationsService pointed at a temp git directory.

    Creates a minimal .git directory so GitOperationsService.__init__
    does not raise ValueError (it only checks for .git existence).
    """
    (tmp_path / ".git").mkdir()
    from code_indexer.global_repos.git_operations import GitOperationsService

    return GitOperationsService(tmp_path)


class TestGetBlameTimeout:
    """Tests for timeout handling in GitOperationsService.get_blame()."""

    def test_get_blame_handles_timeout_expired(self, git_service):
        """get_blame() must return BlameErrorResult when run_git_command raises TimeoutExpired.

        The timeout sentinel is a subprocess.TimeoutExpired raised by run_git_command.
        The method must NOT propagate it; it must return a typed BlameErrorResult.
        """
        from code_indexer.global_repos.git_operations import BlameErrorResult

        with patch(_PATCH_TARGET) as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["git", "blame", "--porcelain", "--", "file.py"],
                timeout=BLAME_TIMEOUT_SECONDS,
            )

            result = git_service.get_blame(path="file.py")

        assert isinstance(result, BlameErrorResult)
        assert result.success is False
        assert (
            result.error == f"Git blame timed out after {BLAME_TIMEOUT_SECONDS} seconds"
        )

    def test_get_blame_passes_timeout_to_run_git_command(self, git_service):
        """get_blame() must pass timeout=BLAME_TIMEOUT_SECONDS to run_git_command.

        Without a timeout the git process can hang indefinitely on repos
        with deep history.
        """
        with patch(_PATCH_TARGET) as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_run.return_value = mock_result

            git_service.get_blame(path="file.py")

        assert mock_run.call_count == 1
        _args, kwargs = mock_run.call_args
        assert kwargs.get("timeout") == BLAME_TIMEOUT_SECONDS, (
            f"run_git_command must be called with timeout={BLAME_TIMEOUT_SECONDS}"
        )
