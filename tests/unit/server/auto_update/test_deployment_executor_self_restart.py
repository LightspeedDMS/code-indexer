"""Unit tests for DeploymentExecutor self-restart mechanism.

Tests the bootstrap problem solution where auto-updater detects changes
to its own code and restarts the service to load new code.
"""

from pathlib import Path
from unittest.mock import Mock, patch, mock_open
import json
import hashlib
import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestAutoUpdateHashCalculation:
    """Test hash calculation for auto_update directory."""

    def test_calculate_auto_update_hash_returns_consistent_hash(self):
        """Hash calculation should return consistent hash for same files."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        # Create mock files with __lt__ for sorting
        mock_file1 = Mock(spec=Path)
        mock_file1.read_text.return_value = "file1 content"
        mock_file1.__lt__ = Mock(return_value=True)

        mock_file2 = Mock(spec=Path)
        mock_file2.read_text.return_value = "file2 content"
        mock_file2.__lt__ = Mock(return_value=False)

        with patch("pathlib.Path.glob") as mock_glob:
            mock_glob.return_value = [mock_file1, mock_file2]

            hash1 = executor._calculate_auto_update_hash()
            hash2 = executor._calculate_auto_update_hash()

            assert hash1 == hash2
            assert isinstance(hash1, str)
            assert len(hash1) == 64  # SHA256 hex digest length

    def test_calculate_auto_update_hash_changes_when_content_changes(self):
        """Hash calculation should return different hash when file content changes."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("pathlib.Path.glob") as mock_glob:
            # First call - original content
            mock_file1 = Mock(spec=Path)
            mock_file1.read_text.return_value = "original content"
            mock_glob.return_value = [mock_file1]

            hash1 = executor._calculate_auto_update_hash()

            # Second call - changed content
            mock_file1.read_text.return_value = "changed content"
            hash2 = executor._calculate_auto_update_hash()

            assert hash1 != hash2

    def test_calculate_auto_update_hash_includes_all_py_files(self):
        """Hash calculation should include all .py files in auto_update directory."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("pathlib.Path.glob") as mock_glob:
            mock_file1 = Mock(spec=Path)
            mock_file1.read_text.return_value = "file1"
            mock_file2 = Mock(spec=Path)
            mock_file2.read_text.return_value = "file2"
            mock_glob.return_value = [mock_file1, mock_file2]

            executor._calculate_auto_update_hash()

            # Should call glob with correct pattern
            mock_glob.assert_called()

    def test_calculate_auto_update_hash_handles_missing_directory(self):
        """Hash calculation should handle missing auto_update directory gracefully."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("pathlib.Path.glob") as mock_glob:
            mock_glob.return_value = []  # No files found

            hash_result = executor._calculate_auto_update_hash()

            # Should return empty string or specific value for empty directory
            assert isinstance(hash_result, str)


class TestAutoUpdateStatusFile:
    """Test status file read/write operations."""

    def test_write_status_file_creates_json_file(self):
        """_write_status_file should create JSON file with correct structure."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("builtins.open", mock_open()) as mock_file:
            with patch("json.dump") as mock_json_dump:
                executor._write_status_file("pending_restart", "Testing restart")

                # Note: Using /var/lib/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
                mock_file.assert_called_once_with(
                    Path("/var/lib/cidx-auto-update-status.json"), "w"
                )
                # Check that json.dump was called with correct structure
                call_args = mock_json_dump.call_args[0]
                status_data = call_args[0]
                assert status_data["status"] == "pending_restart"
                assert status_data["details"] == "Testing restart"
                assert "timestamp" in status_data

    def test_write_status_file_includes_version(self):
        """_write_status_file should include version in status data."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("builtins.open", mock_open()):
            with patch("json.dump") as mock_json_dump:
                executor._write_status_file("in_progress")

                call_args = mock_json_dump.call_args[0]
                status_data = call_args[0]
                assert "version" in status_data

    def test_write_status_file_handles_io_error_gracefully(self):
        """_write_status_file should handle IO errors without crashing."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("builtins.open", side_effect=IOError("Disk full")):
            # Should not raise exception
            executor._write_status_file("failed", "Test error")

    def test_read_status_file_returns_dict(self):
        """_read_status_file should return dict with status data."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        status_data = {
            "status": "pending_restart",
            "version": "8.8.0",
            "timestamp": "2026-02-08T10:00:00",
            "details": "Auto-update detected",
        }

        with patch("pathlib.Path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(status_data))):
                result = executor._read_status_file()

                assert result is not None
                assert result["status"] == "pending_restart"
                assert result["version"] == "8.8.0"

    def test_read_status_file_returns_none_when_file_missing(self):
        """_read_status_file should return None when file doesn't exist."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("pathlib.Path.exists", return_value=False):
            result = executor._read_status_file()

            assert result is None

    def test_read_status_file_returns_none_on_json_error(self):
        """_read_status_file should return None on malformed JSON."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("pathlib.Path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="invalid json {")):
                result = executor._read_status_file()

                assert result is None


class TestAutoUpdateSelfRestartDetection:
    """Test self-restart detection in execute() method."""

    @patch("subprocess.run")
    def test_execute_detects_auto_update_code_change(self, mock_run):
        """execute() should detect when auto_update code itself changes."""
        mock_run.return_value = Mock(returncode=0, stdout="Success")

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        # Mock hash calculation to simulate change
        with patch.object(executor, "_calculate_auto_update_hash") as mock_hash:
            mock_hash.side_effect = ["hash_before", "hash_after"]  # Different hashes

            with patch.object(executor, "_write_status_file") as mock_write_status:
                with patch.object(
                    executor, "_restart_auto_update_service"
                ) as mock_restart:
                    result = executor.execute()

                    # Should detect change and restart
                    mock_write_status.assert_called()
                    mock_restart.assert_called_once()
                    assert result is True

    @patch("subprocess.run")
    def test_execute_continues_when_auto_update_unchanged(self, mock_run):
        """execute() should continue normal deployment when auto_update unchanged."""
        mock_run.return_value = Mock(returncode=0, stdout="Success")

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        # Mock hash calculation to simulate no change
        with patch.object(executor, "_calculate_auto_update_hash") as mock_hash:
            mock_hash.return_value = "same_hash"  # Same hash before and after

            with patch.object(
                executor, "_restart_auto_update_service"
            ) as mock_restart:
                result = executor.execute()

                # Should NOT restart, should continue deployment
                mock_restart.assert_not_called()
                # Should complete full deployment (git pull, pip install, etc.)
                assert result is True

    @patch("subprocess.run")
    def test_execute_writes_pending_restart_status_before_restart(self, mock_run):
        """execute() should write pending_restart status before restarting."""
        mock_run.return_value = Mock(returncode=0, stdout="Success")

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_calculate_auto_update_hash") as mock_hash:
            mock_hash.side_effect = ["hash1", "hash2"]

            with patch.object(executor, "_write_status_file") as mock_write:
                with patch.object(executor, "_restart_auto_update_service"):
                    executor.execute()

                    # Should write pending_restart status
                    mock_write.assert_called()
                    call_args = mock_write.call_args[0]
                    assert call_args[0] == "pending_restart"


class TestAutoUpdateServiceRestart:
    """Test _restart_auto_update_service method."""

    @patch("subprocess.run")
    def test_restart_auto_update_service_executes_systemctl(self, mock_run):
        """_restart_auto_update_service should execute systemctl restart command."""
        mock_run.return_value = Mock(returncode=0)

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))
        result = executor._restart_auto_update_service()

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["sudo", "systemctl", "restart", "cidx-auto-update"]

    @patch("subprocess.run")
    def test_restart_auto_update_service_returns_false_on_failure(self, mock_run):
        """_restart_auto_update_service should return False when command fails."""
        mock_run.return_value = Mock(returncode=1, stderr="Service not found")

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))
        result = executor._restart_auto_update_service()

        assert result is False

    @patch("subprocess.run")
    def test_restart_auto_update_service_handles_exception(self, mock_run):
        """_restart_auto_update_service should handle exceptions gracefully."""
        mock_run.side_effect = Exception("Unexpected error")

        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))
        result = executor._restart_auto_update_service()

        assert result is False


class TestAutoUpdateRetryOnStartup:
    """Test _should_retry_on_startup method."""

    def test_should_retry_when_status_is_pending_restart(self):
        """_should_retry_on_startup should return True when status is pending_restart."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_read_status_file") as mock_read:
            mock_read.return_value = {"status": "pending_restart"}

            result = executor._should_retry_on_startup()

            assert result is True

    def test_should_retry_when_status_is_failed(self):
        """_should_retry_on_startup should return True when status is failed."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_read_status_file") as mock_read:
            mock_read.return_value = {"status": "failed"}

            result = executor._should_retry_on_startup()

            assert result is True

    def test_should_not_retry_when_status_is_success(self):
        """_should_retry_on_startup should return False when status is success."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_read_status_file") as mock_read:
            mock_read.return_value = {"status": "success"}

            result = executor._should_retry_on_startup()

            assert result is False

    def test_should_not_retry_when_no_status_file(self):
        """_should_retry_on_startup should return False when no status file exists."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_read_status_file") as mock_read:
            mock_read.return_value = None

            result = executor._should_retry_on_startup()

            assert result is False

    def test_should_not_retry_when_status_file_corrupted(self):
        """_should_retry_on_startup should return False when status file is corrupted."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch.object(executor, "_read_status_file") as mock_read:
            mock_read.return_value = {"corrupted": "data"}  # Missing 'status' key

            result = executor._should_retry_on_startup()

            assert result is False
