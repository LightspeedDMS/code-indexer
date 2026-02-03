"""
Tests for git safe.directory configuration in auto-update.

Tests for DeploymentExecutor._ensure_git_safe_directory() method that adds
the repository to git's safe.directory configuration to avoid "dubious ownership"
errors when the service runs as a different user than the repo owner.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureGitSafeDirectory:
    """Tests for _ensure_git_safe_directory method."""

    def test_ensure_git_safe_directory_method_exists(self):
        """DeploymentExecutor should have _ensure_git_safe_directory method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_git_safe_directory")
        assert callable(getattr(executor, "_ensure_git_safe_directory"))

    def test_ensure_git_safe_directory_returns_true_when_service_not_found(self):
        """Should return True when service file doesn't exist (not a fatal error)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        # Mock Path.exists to return False
        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_git_safe_directory()

        assert result is True

    def test_ensure_git_safe_directory_returns_true_when_already_configured(self):
        """Should return True without changes if safe.directory already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        # Mock git config --get-all to show repo already configured
        git_output = "/home/user/code-indexer\n/some/other/path\n"

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (shows already configured)
                    mock_run.return_value = MagicMock(
                        returncode=0,
                        stdout=git_output,
                    )

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify we only checked, didn't try to add
        assert mock_run.call_count == 1
        check_call = mock_run.call_args_list[0]
        assert "git" in check_call[0][0]
        assert "config" in check_call[0][0]
        assert "--get-all" in check_call[0][0]

    def test_ensure_git_safe_directory_adds_when_not_configured(self):
        """Should add safe.directory when not already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (empty, not configured)
                    # Second call: git config --add (success)
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),  # Not configured
                        MagicMock(returncode=0),  # Add successful
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify we checked and then added
        assert mock_run.call_count == 2

        # Check call
        check_call = mock_run.call_args_list[0]
        assert check_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--get-all",
            "safe.directory",
        ]

        # Add call
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            "/home/user/code-indexer",
        ]

    def test_ensure_git_safe_directory_skips_when_no_user_line(self):
        """Should skip gracefully when service file has no User= line."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                result = executor._ensure_git_safe_directory()

        # Should return True (not a fatal error, just skip)
        assert result is True

    def test_ensure_git_safe_directory_handles_git_config_failure(self):
        """Should return False when git config command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (success, not configured)
                    # Second call: git config --add (FAILURE)
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(
                            returncode=1,
                            stderr="fatal: unable to write new config",
                        ),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is False

    def test_ensure_git_safe_directory_handles_exception(self):
        """Should return False and log error on exception."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", side_effect=Exception("Disk failure")):
            result = executor._ensure_git_safe_directory()

        assert result is False

    def test_ensure_git_safe_directory_uses_working_directory_from_service_file(self):
        """Should use WorkingDirectory from service file as repo path."""
        executor = DeploymentExecutor(
            repo_path=Path("/wrong/path"),  # This should be ignored
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/correct/repo/path
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(returncode=0),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify correct repo path was used
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0][-1] == "/correct/repo/path"

    def test_ensure_git_safe_directory_falls_back_to_repo_path_when_no_working_directory(
        self,
    ):
        """Should fall back to self.repo_path when WorkingDirectory not in service file."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(returncode=0),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify fallback to self.repo_path
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0][-1] == "/home/user/code-indexer"
