"""
Tests for CIDX_REPO_ROOT environment variable in auto-update.

Tests for DeploymentExecutor._ensure_cidx_repo_root() method that adds
Environment="CIDX_REPO_ROOT={repo_path}" to existing systemd service files during auto-update.
This is required for self-monitoring to detect the repository root directory.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureCidxRepoRoot:
    """Tests for _ensure_cidx_repo_root method."""

    def test_ensure_cidx_repo_root_method_exists(self):
        """DeploymentExecutor should have _ensure_cidx_repo_root method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_cidx_repo_root")
        assert callable(getattr(executor, "_ensure_cidx_repo_root"))

    def test_ensure_cidx_repo_root_returns_true_when_service_not_found(self):
        """Should return True when service file doesn't exist (not an error)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        # Mock Path.exists to return False
        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_cidx_repo_root()

        assert result is True

    def test_ensure_cidx_repo_root_returns_true_when_already_present(self):
        """Should return True without changes if CIDX_REPO_ROOT already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
Environment="VOYAGE_API_KEY=test-key"
Environment="CIDX_REPO_ROOT=/home/user/code-indexer"
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                result = executor._ensure_cidx_repo_root()

        assert result is True

    def test_ensure_cidx_repo_root_adds_environment_variable_after_last_env_line(
        self,
    ):
        """Should add CIDX_REPO_ROOT after the last Environment= line."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
Environment="VOYAGE_API_KEY=test-key"
Environment="CIDX_SERVER_MODE=1"
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        expected_content = """[Service]
Environment="VOYAGE_API_KEY=test-key"
Environment="CIDX_SERVER_MODE=1"
Environment="CIDX_REPO_ROOT=/home/user/code-indexer"
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # Mock successful tee and daemon-reload
                    mock_run.return_value = MagicMock(returncode=0)

                    result = executor._ensure_cidx_repo_root()

        assert result is True
        # Verify sudo tee was called with correct content
        assert mock_run.call_count == 2  # tee + daemon-reload
        tee_call = mock_run.call_args_list[0]
        assert tee_call[0][0] == [
            "sudo",
            "tee",
            "/etc/systemd/system/cidx-server.service",
        ]
        assert tee_call[1]["input"] == expected_content

        # Verify daemon-reload was called
        daemon_reload_call = mock_run.call_args_list[1]
        assert daemon_reload_call[0][0] == ["sudo", "systemctl", "daemon-reload"]

    def test_ensure_cidx_repo_root_handles_write_failure(self):
        """Should return False when tee command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
Environment="VOYAGE_API_KEY=test-key"
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # Mock failed tee
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="Permission denied",
                    )

                    result = executor._ensure_cidx_repo_root()

        assert result is False

    def test_ensure_cidx_repo_root_handles_exception(self):
        """Should return False and log error on exception."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", side_effect=Exception("Disk failure")):
            result = executor._ensure_cidx_repo_root()

        assert result is False

    def test_ensure_cidx_repo_root_no_environment_lines_present(self):
        """Should handle service file with no Environment= lines by inserting before ExecStart."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
WorkingDirectory=/home/user
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        expected_content = """[Service]
WorkingDirectory=/home/user
Environment="CIDX_REPO_ROOT=/home/user/code-indexer"
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)

                    result = executor._ensure_cidx_repo_root()

        assert result is True
        tee_call = mock_run.call_args_list[0]
        assert tee_call[1]["input"] == expected_content

    def test_ensure_cidx_repo_root_no_insertion_point(self):
        """Should return True with warning when no valid insertion point exists."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        # Service file with no Environment= and no ExecStart=
        service_content = """[Service]
WorkingDirectory=/home/user
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("code_indexer.server.auto_update.deployment_executor.logger") as mock_logger:
                    result = executor._ensure_cidx_repo_root()

        # Should return True (graceful handling, not fatal)
        assert result is True
        # Should log warning with DEPLOY-GENERAL-023
        mock_logger.warning.assert_called_once()
        warning_call = mock_logger.warning.call_args[0][0]
        assert "DEPLOY-GENERAL-023" in warning_call
        assert "Could not find insertion point for CIDX_REPO_ROOT" in warning_call
