"""Tests for DeploymentExecutor Python environment detection (Issue #154)."""

from code_indexer.server.auto_update.deployment_executor import (
    DeploymentExecutor,
    PENDING_REDEPLOY_MARKER,
    AUTO_UPDATE_SERVICE_NAME,
)
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
import sys


@pytest.fixture
def executor():
    """Create DeploymentExecutor instance for testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


class TestGetServerPython:
    """Tests for _get_server_python() method."""

    def test_get_server_python_parses_execstart_correctly(self, executor):
        """Test that _get_server_python correctly parses ExecStart line."""
        service_content = """
[Unit]
Description=CIDX Server

[Service]
User=code-indexer
WorkingDirectory=/opt/code-indexer
ExecStart=/opt/pipx/venvs/code-indexer/bin/python -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=service_content,
                stderr=""
            )

            # Mock Path.exists to return True for Python path
            with patch("pathlib.Path.exists", return_value=True):
                result = executor._get_server_python()

        assert result == "/opt/pipx/venvs/code-indexer/bin/python"
        mock_run.assert_called_once()

    def test_get_server_python_handles_missing_file(self, executor):
        """Test that _get_server_python falls back to sys.executable when service file is missing."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="",
                stderr="cat: /etc/systemd/system/cidx-server.service: No such file or directory"
            )

            result = executor._get_server_python()

        assert result == sys.executable
        mock_run.assert_called_once()

    def test_get_server_python_handles_malformed_service(self, executor):
        """Test that _get_server_python falls back when ExecStart line is missing."""
        service_content = """
[Unit]
Description=CIDX Server

[Service]
User=code-indexer
WorkingDirectory=/opt/code-indexer
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=service_content,
                stderr=""
            )

            result = executor._get_server_python()

        assert result == sys.executable
        mock_run.assert_called_once()

    def test_get_server_python_handles_nonexistent_python_path(self, executor):
        """Test that _get_server_python falls back when Python path doesn't exist."""
        service_content = """
[Unit]
Description=CIDX Server

[Service]
ExecStart=/nonexistent/python -m uvicorn code_indexer.server.app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=service_content,
                stderr=""
            )

            # Mock Path.exists to return False for nonexistent Python path
            with patch("pathlib.Path.exists", return_value=False):
                result = executor._get_server_python()

        assert result == sys.executable
        mock_run.assert_called_once()

    def test_get_server_python_handles_subprocess_timeout(self, executor):
        """Test that _get_server_python handles subprocess timeout gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Timeout reading service file")

            result = executor._get_server_python()

        assert result == sys.executable
        mock_run.assert_called_once()

    def test_get_server_python_with_complex_execstart(self, executor):
        """Test parsing ExecStart with multiple flags and arguments."""
        service_content = """
[Service]
ExecStart=/opt/pipx/venvs/code-indexer/bin/python -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000 --workers 1
"""

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout=service_content,
                stderr=""
            )

            with patch("pathlib.Path.exists", return_value=True):
                result = executor._get_server_python()

        assert result == "/opt/pipx/venvs/code-indexer/bin/python"


class TestEnsureAutoUpdaterUsesServerPython:
    """Tests for _ensure_auto_updater_uses_server_python() method."""

    def test_ensure_auto_updater_no_changes_needed(self, executor):
        """Test that no changes are made when auto-updater already uses correct Python."""
        server_python = "/opt/pipx/venvs/code-indexer/bin/python"
        auto_update_service_content = f"""
[Unit]
Description=CIDX Auto-Update Service

[Service]
ExecStart={server_python} /opt/code-indexer/auto_update_script.py
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch.object(executor, "_get_server_python", return_value=server_python):
            with patch("subprocess.run") as mock_run:
                # First call: read auto-updater service file
                mock_run.return_value = Mock(
                    returncode=0,
                    stdout=auto_update_service_content,
                    stderr=""
                )

                with patch("pathlib.Path.touch") as mock_touch:
                    result = executor._ensure_auto_updater_uses_server_python()

        assert result is True
        # Should not create marker since no changes needed
        mock_touch.assert_not_called()

    def test_ensure_auto_updater_updates_and_creates_marker(self, executor):
        """Test that auto-updater service is updated and marker is created."""
        server_python = "/opt/pipx/venvs/code-indexer/bin/python"
        wrong_python = "/usr/bin/python3"

        old_service_content = f"""
[Unit]
Description=CIDX Auto-Update Service

[Service]
ExecStart={wrong_python} /opt/code-indexer/auto_update_script.py
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch.object(executor, "_get_server_python", return_value=server_python):
            with patch("subprocess.run") as mock_run:
                # Setup mock returns for: read, tee, daemon-reload
                mock_run.side_effect = [
                    Mock(returncode=0, stdout=old_service_content, stderr=""),  # cat
                    Mock(returncode=0, stdout="", stderr=""),  # sudo tee
                    Mock(returncode=0, stdout="", stderr=""),  # daemon-reload
                ]

                with patch("pathlib.Path.touch") as mock_touch:
                    result = executor._ensure_auto_updater_uses_server_python()

        assert result is True
        # Verify marker was created
        mock_touch.assert_called_once()

        # Verify sudo tee was called with updated content
        calls = mock_run.call_args_list
        assert len(calls) == 3

        # Check that tee was called with correct content
        tee_call = calls[1]
        assert "sudo" in tee_call[0][0]
        assert "tee" in tee_call[0][0]

        # Verify the Python path was updated correctly
        actual_content = tee_call[1]["input"]
        assert server_python in actual_content
        assert wrong_python not in actual_content
        assert "ExecStart" in actual_content

    def test_ensure_auto_updater_handles_missing_service_file(self, executor):
        """Test handling when auto-updater service file doesn't exist."""
        with patch.object(executor, "_get_server_python", return_value="/opt/pipx/venvs/code-indexer/bin/python"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=1,
                    stdout="",
                    stderr="cat: cannot open file"
                )

                result = executor._ensure_auto_updater_uses_server_python()

        assert result is False

    def test_ensure_auto_updater_handles_tee_failure(self, executor):
        """Test handling when sudo tee fails to write service file."""
        server_python = "/opt/pipx/venvs/code-indexer/bin/python"
        wrong_python = "/usr/bin/python3"

        old_service_content = f"""
[Service]
ExecStart={wrong_python} /opt/code-indexer/auto_update_script.py
"""

        with patch.object(executor, "_get_server_python", return_value=server_python):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    Mock(returncode=0, stdout=old_service_content, stderr=""),  # cat
                    Mock(returncode=1, stdout="", stderr="Permission denied"),  # sudo tee fails
                ]

                result = executor._ensure_auto_updater_uses_server_python()

        assert result is False

    def test_ensure_auto_updater_handles_daemon_reload_failure(self, executor):
        """Test handling when daemon-reload fails."""
        server_python = "/opt/pipx/venvs/code-indexer/bin/python"
        wrong_python = "/usr/bin/python3"

        old_service_content = f"""
[Service]
ExecStart={wrong_python} /opt/code-indexer/auto_update_script.py
"""

        with patch.object(executor, "_get_server_python", return_value=server_python):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    Mock(returncode=0, stdout=old_service_content, stderr=""),  # cat
                    Mock(returncode=0, stdout="", stderr=""),  # sudo tee
                    Mock(returncode=1, stdout="", stderr="Failed to reload daemon"),  # daemon-reload fails
                ]

                result = executor._ensure_auto_updater_uses_server_python()

        assert result is False

    def test_ensure_auto_updater_handles_exception(self, executor):
        """Test exception handling in _ensure_auto_updater_uses_server_python."""
        with patch.object(executor, "_get_server_python", side_effect=Exception("Unexpected error")):
            result = executor._ensure_auto_updater_uses_server_python()

        assert result is False


class TestPipInstallUsesServerPython:
    """Tests for pip_install() using server Python."""

    def test_pip_install_uses_server_python(self, executor):
        """Test that pip_install uses _get_server_python() instead of sys.executable."""
        server_python = "/opt/pipx/venvs/code-indexer/bin/python"

        with patch.object(executor, "_get_server_python", return_value=server_python) as mock_get_python:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0,
                    stdout="Successfully installed code-indexer",
                    stderr=""
                )

                result = executor.pip_install()

        assert result is True
        mock_get_python.assert_called_once()

        # Verify subprocess.run was called with sudo and server Python
        # Uses sudo because pipx venv may be owned by root (e.g., /opt/pipx/venvs/)
        calls = mock_run.call_args_list
        assert len(calls) == 1
        call_args = calls[0][0][0]
        assert call_args[0] == "sudo"
        assert call_args[1] == server_python
        assert call_args[2:] == ["-m", "pip", "install", "--break-system-packages", "-e", "."]

    def test_pip_install_falls_back_when_get_server_python_fails(self, executor):
        """Test that pip_install still works when _get_server_python fails."""
        # _get_server_python returns sys.executable on failure
        with patch.object(executor, "_get_server_python", return_value=sys.executable):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0,
                    stdout="Successfully installed",
                    stderr=""
                )

                result = executor.pip_install()

        assert result is True


class TestExecuteCallsEnsureAutoUpdater:
    """Tests for execute() calling _ensure_auto_updater_uses_server_python()."""

    @patch.object(DeploymentExecutor, "_ensure_auto_updater_uses_server_python", return_value=True)
    @patch.object(DeploymentExecutor, "ensure_ripgrep", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_git_safe_directory", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_cidx_repo_root", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_workers_config", return_value=True)
    @patch.object(DeploymentExecutor, "pip_install", return_value=True)
    @patch.object(DeploymentExecutor, "build_custom_hnswlib", return_value=True)
    @patch.object(DeploymentExecutor, "git_submodule_update", return_value=True)
    @patch.object(DeploymentExecutor, "_calculate_auto_update_hash", return_value="same_hash")
    @patch.object(DeploymentExecutor, "git_pull", return_value=True)
    def test_execute_calls_ensure_auto_updater(
        self,
        mock_git_pull,
        mock_calc_hash,
        mock_git_submodule,
        mock_build_hnswlib,
        mock_pip_install,
        mock_ensure_workers,
        mock_ensure_cidx_repo,
        mock_ensure_git_safe,
        mock_ensure_ripgrep,
        mock_ensure_auto_updater,
        executor,
    ):
        """Test that execute() calls _ensure_auto_updater_uses_server_python()."""
        result = executor.execute()

        assert result is True
        mock_ensure_auto_updater.assert_called_once()

    @patch.object(DeploymentExecutor, "_ensure_auto_updater_uses_server_python", return_value=False)
    @patch.object(DeploymentExecutor, "ensure_ripgrep", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_git_safe_directory", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_cidx_repo_root", return_value=True)
    @patch.object(DeploymentExecutor, "_ensure_workers_config", return_value=True)
    @patch.object(DeploymentExecutor, "pip_install", return_value=True)
    @patch.object(DeploymentExecutor, "build_custom_hnswlib", return_value=True)
    @patch.object(DeploymentExecutor, "git_submodule_update", return_value=True)
    @patch.object(DeploymentExecutor, "_calculate_auto_update_hash", return_value="same_hash")
    @patch.object(DeploymentExecutor, "git_pull", return_value=True)
    def test_execute_continues_on_ensure_auto_updater_failure(
        self,
        mock_git_pull,
        mock_calc_hash,
        mock_git_submodule,
        mock_build_hnswlib,
        mock_pip_install,
        mock_ensure_workers,
        mock_ensure_cidx_repo,
        mock_ensure_git_safe,
        mock_ensure_ripgrep,
        mock_ensure_auto_updater,
        executor,
    ):
        """Test that execute() continues even if _ensure_auto_updater_uses_server_python fails."""
        result = executor.execute()

        # execute() should still return True overall (non-fatal error)
        assert result is True


class TestConstants:
    """Tests for module constants."""

    def test_pending_redeploy_marker_constant(self):
        """Test that PENDING_REDEPLOY_MARKER constant is defined."""
        # Note: Using /var/lib/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
        assert PENDING_REDEPLOY_MARKER == Path("/var/lib/cidx-pending-redeploy")

    def test_auto_update_service_name_constant(self):
        """Test that AUTO_UPDATE_SERVICE_NAME constant is defined."""
        assert AUTO_UPDATE_SERVICE_NAME == "cidx-auto-update"
