"""
Unit tests for git_commit committer identity parameters (Story #402).

Tests that git_commit service method:
- Accepts optional committer_email and committer_name parameters
- Sets GIT_COMMITTER_EMAIL when committer_email is provided
- Sets GIT_COMMITTER_NAME when committer_name is provided
- Falls back to author identity (GIT_AUTHOR_*) when committer params are None
- Handles partial committer (email only, no name)
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def git_ops_service():
    """Create GitOperationsService bypassing __init__ (same pattern as test_git_push_with_pat.py)."""
    from code_indexer.server.services.git_operations_service import GitOperationsService

    service = GitOperationsService.__new__(GitOperationsService)
    timeouts = MagicMock()
    timeouts.git_local_timeout = 30
    service._git_timeouts = timeouts
    return service


class TestGitCommitCommitterIdentityParams:
    """Test committer_email / committer_name parameters in git_commit (Story #402)."""

    def test_git_commit_with_committer_email_and_name_sets_committer_env_vars(
        self, git_ops_service
    ):
        """When committer_email and committer_name are provided, GIT_COMMITTER_* is set."""
        captured_env = {}

        def mock_run(cmd, **kwargs):
            if "diff" in cmd:
                return Mock(returncode=0, stdout="file.txt\n", stderr="")
            if "commit" in cmd and "-m" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] msg", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc1234567890", stderr="")
            if "show" in cmd:
                return Mock(returncode=0, stdout="alice@gitlab.com", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            result = git_ops_service.git_commit(
                Path("/tmp/repo"),
                message="Test commit",
                user_email="user@example.com",
                user_name="User Name",
                committer_email="alice@gitlab.com",
                committer_name="Alice Smith",
            )

        assert result["success"] is True
        assert captured_env.get("GIT_COMMITTER_EMAIL") == "alice@gitlab.com"
        assert captured_env.get("GIT_COMMITTER_NAME") == "Alice Smith"

    def test_git_commit_with_committer_email_and_name_preserves_author_identity(
        self, git_ops_service
    ):
        """Author identity remains the user identity even when committer is explicitly set."""
        captured_env = {}

        def mock_run(cmd, **kwargs):
            if "diff" in cmd:
                return Mock(returncode=0, stdout="file.txt\n", stderr="")
            if "commit" in cmd and "-m" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] msg", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc1234567890", stderr="")
            if "show" in cmd:
                return Mock(returncode=0, stdout="alice@gitlab.com", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            git_ops_service.git_commit(
                Path("/tmp/repo"),
                message="Test",
                user_email="user@example.com",
                user_name="User Name",
                committer_email="alice@gitlab.com",
                committer_name="Alice Smith",
            )

        assert captured_env.get("GIT_AUTHOR_EMAIL") == "user@example.com"
        assert captured_env.get("GIT_AUTHOR_NAME") == "User Name"

    def test_git_commit_without_committer_params_sets_committer_from_author(
        self, git_ops_service
    ):
        """When committer_email/name are None, GIT_COMMITTER_* uses author identity."""
        captured_env = {}

        def mock_run(cmd, **kwargs):
            if "diff" in cmd:
                return Mock(returncode=0, stdout="file.txt\n", stderr="")
            if "commit" in cmd and "-m" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] msg", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc1234567890", stderr="")
            if "show" in cmd:
                return Mock(returncode=0, stdout="user@example.com", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            git_ops_service.git_commit(
                Path("/tmp/repo"),
                message="Test",
                user_email="user@example.com",
                user_name="User Name",
            )

        assert captured_env.get("GIT_COMMITTER_EMAIL") == "user@example.com"
        assert captured_env.get("GIT_COMMITTER_NAME") == "User Name"

    def test_git_commit_with_committer_email_only_sets_email_name_falls_back_to_author(
        self, git_ops_service
    ):
        """Partial credential: committer_email set, committer_name=None -> email from credential, name from author."""
        captured_env = {}

        def mock_run(cmd, **kwargs):
            if "diff" in cmd:
                return Mock(returncode=0, stdout="file.txt\n", stderr="")
            if "commit" in cmd and "-m" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] msg", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc1234567890", stderr="")
            if "show" in cmd:
                return Mock(returncode=0, stdout="bob@gitlab.com", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            git_ops_service.git_commit(
                Path("/tmp/repo"),
                message="Test",
                user_email="user@example.com",
                user_name="User Name",
                committer_email="bob@gitlab.com",
                committer_name=None,
            )

        assert captured_env.get("GIT_COMMITTER_EMAIL") == "bob@gitlab.com"
        assert captured_env.get("GIT_COMMITTER_NAME") == "User Name"

    def test_git_commit_backward_compat_positional_args_still_work(
        self, git_ops_service
    ):
        """git_commit with only positional args (no committer params) still works correctly."""
        captured_env = {}

        def mock_run(cmd, **kwargs):
            if "diff" in cmd:
                return Mock(returncode=0, stdout="file.txt\n", stderr="")
            if "commit" in cmd and "-m" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return Mock(returncode=0, stdout="[main abc] msg", stderr="")
            if "rev-parse" in cmd:
                return Mock(returncode=0, stdout="abc1234567890", stderr="")
            if "show" in cmd:
                return Mock(returncode=0, stdout="user@example.com", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run,
        ):
            result = git_ops_service.git_commit(
                Path("/tmp/repo"),
                "Test commit",
                "user@example.com",
                "User Name",
            )

        assert result["success"] is True
        assert captured_env.get("GIT_AUTHOR_EMAIL") == "user@example.com"
        assert captured_env.get("GIT_AUTHOR_NAME") == "User Name"
        assert captured_env.get("GIT_COMMITTER_EMAIL") == "user@example.com"
        assert captured_env.get("GIT_COMMITTER_NAME") == "User Name"
