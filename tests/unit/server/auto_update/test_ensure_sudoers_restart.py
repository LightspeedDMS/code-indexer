"""
Tests for sudoers restart rule configuration in auto-update.

Tests for DeploymentExecutor._ensure_sudoers_restart() method that creates
a sudoers rule to allow the service user to restart the systemd service
without a password prompt.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureSudoersRestart:
    """Tests for _ensure_sudoers_restart method."""

    def test_ensure_sudoers_already_configured(self):
        """Should return True without changes if sudoers rule already exists with correct content."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=jsbattig
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        expected_rule = "jsbattig ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart cidx-server"

        with patch.object(Path, "exists") as mock_exists:
            # Service file exists, sudoers file exists
            mock_exists.side_effect = lambda: True
            with patch.object(Path, "read_text") as mock_read:
                # First call: service file content
                # Second call: sudoers file content (already correct)
                mock_read.side_effect = [service_content, expected_rule]

                result = executor._ensure_sudoers_restart()

        assert result is True

    def test_ensure_sudoers_creates_rule(self):
        """Should create sudoers rule when not already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=jsbattig
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        expected_rule = "jsbattig ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart cidx-server"

        with patch.object(Path, "exists") as mock_exists:
            # Service file exists, sudoers file doesn't exist
            mock_exists.side_effect = [True, False]
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: sudo tee (create file)
                    # Second call: sudo chmod 0440
                    # Third call: sudo visudo -c -f (validate)
                    mock_run.side_effect = [
                        MagicMock(returncode=0),  # tee success
                        MagicMock(returncode=0),  # chmod success
                        MagicMock(returncode=0),  # visudo success
                    ]

                    result = executor._ensure_sudoers_restart()

        assert result is True
        assert mock_run.call_count == 3

        # Verify tee call
        tee_call = mock_run.call_args_list[0]
        assert tee_call[0][0] == ["sudo", "tee", "/etc/sudoers.d/cidx-server"]
        assert tee_call[1]["input"] == expected_rule

        # Verify chmod call
        chmod_call = mock_run.call_args_list[1]
        assert chmod_call[0][0] == ["sudo", "chmod", "0440", "/etc/sudoers.d/cidx-server"]

        # Verify visudo call
        visudo_call = mock_run.call_args_list[2]
        assert visudo_call[0][0] == ["sudo", "visudo", "-c", "-f", "/etc/sudoers.d/cidx-server"]

    def test_ensure_sudoers_no_service_file(self):
        """Should return True when service file doesn't exist (not a fatal error)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        # Mock Path.exists to return False for service file
        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_sudoers_restart()

        assert result is True

    def test_ensure_sudoers_no_user_in_service(self):
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
                result = executor._ensure_sudoers_restart()

        # Should return True (not a fatal error, just skip)
        assert result is True

    def test_ensure_sudoers_visudo_validation_fails(self):
        """Should return False and remove file when visudo validation fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=jsbattig
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists") as mock_exists:
            # Service file exists, sudoers file doesn't exist
            mock_exists.side_effect = [True, False]
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: sudo tee (success)
                    # Second call: sudo chmod 0440 (success)
                    # Third call: sudo visudo -c -f (FAILURE)
                    # Fourth call: sudo rm -f (cleanup)
                    mock_run.side_effect = [
                        MagicMock(returncode=0),  # tee success
                        MagicMock(returncode=0),  # chmod success
                        MagicMock(
                            returncode=1,
                            stderr="parse error in /etc/sudoers.d/cidx-server",
                        ),  # visudo failure
                        MagicMock(returncode=0),  # rm success
                    ]

                    result = executor._ensure_sudoers_restart()

        assert result is False
        # Verify cleanup call happened
        assert mock_run.call_count == 4
        cleanup_call = mock_run.call_args_list[3]
        assert cleanup_call[0][0] == ["sudo", "rm", "-f", "/etc/sudoers.d/cidx-server"]

    def test_ensure_sudoers_creation_fails(self):
        """Should return False when sudo tee command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=jsbattig
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists") as mock_exists:
            # Service file exists, sudoers file doesn't exist
            mock_exists.side_effect = [True, False]
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: sudo tee (FAILURE)
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="tee: /etc/sudoers.d/cidx-server: Permission denied",
                    )

                    result = executor._ensure_sudoers_restart()

        assert result is False
        # Should only have tried tee, not chmod or visudo
        assert mock_run.call_count == 1
