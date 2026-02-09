"""
Unit tests for ripgrep installation in DeploymentExecutor.

Tests that DeploymentExecutor.ensure_ripgrep() properly delegates to the shared
RipgrepInstaller utility class. The actual ripgrep installation logic is
comprehensively tested in tests/unit/server/utils/test_ripgrep_installer.py.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureRipgrepDelegation:
    """Tests for ensure_ripgrep delegation to RipgrepInstaller."""

    @pytest.fixture
    def executor(self, tmp_path):
        """Create executor with temporary directory."""
        return DeploymentExecutor(
            repo_path=tmp_path / "repo",
            service_name="cidx-server",
            server_url="http://localhost:8000",
        )

    def test_delegates_to_ripgrep_installer(self, executor):
        """Test ensure_ripgrep creates RipgrepInstaller and calls install()."""
        with patch(
            "code_indexer.server.auto_update.deployment_executor.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = True
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = executor.ensure_ripgrep()

        # Verify RipgrepInstaller was instantiated with home_dir parameter
        # (Bug #155 fix: now passes service user's home or None)
        mock_ripgrep_class.assert_called_once()
        call_kwargs = mock_ripgrep_class.call_args.kwargs
        assert "home_dir" in call_kwargs
        # home_dir can be None or a Path, depending on whether service file exists
        assert call_kwargs["home_dir"] is None or isinstance(
            call_kwargs["home_dir"], Path
        )

        # Verify install() was called on the instance
        mock_ripgrep_instance.install.assert_called_once()

        # Verify result is propagated
        assert result is True

    def test_returns_true_when_ripgrep_installer_succeeds(self, executor):
        """Test returns True when RipgrepInstaller.install() returns True."""
        with patch(
            "code_indexer.server.auto_update.deployment_executor.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = True
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = executor.ensure_ripgrep()

        assert result is True

    def test_returns_false_when_ripgrep_installer_fails(self, executor):
        """Test returns False when RipgrepInstaller.install() returns False."""
        with patch(
            "code_indexer.server.auto_update.deployment_executor.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = False
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = executor.ensure_ripgrep()

        assert result is False

    def test_passes_service_user_home_to_ripgrep_installer(self, executor):
        """Test ensure_ripgrep passes service user's home directory to RipgrepInstaller.

        Bug #155: RipgrepInstaller() was called without home_dir parameter, causing
        ripgrep to be installed in wrong location on production servers where service
        runs as non-root user (e.g., code-indexer).
        """
        expected_home = Path("/home/code-indexer")

        # Use flattened context managers for better readability
        with patch(
            "code_indexer.server.auto_update.deployment_executor.RipgrepInstaller"
        ) as mock_ripgrep_class, patch.object(
            executor, "_get_service_user_home", return_value=expected_home
        ):
            # Setup mocks
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = True
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            # Execute
            result = executor.ensure_ripgrep()

        # Verify RipgrepInstaller was called with service user's home directory
        mock_ripgrep_class.assert_called_once_with(home_dir=expected_home)
        mock_ripgrep_instance.install.assert_called_once()
        assert result is True


class TestExecuteMethodIntegration:
    """Tests for execute() method integration with ripgrep installation."""

    @pytest.fixture
    def executor(self, tmp_path):
        """Create executor with temporary directory."""
        return DeploymentExecutor(
            repo_path=tmp_path / "repo",
            service_name="cidx-server",
            server_url="http://localhost:8000",
        )

    def test_execute_calls_ensure_ripgrep(self, executor, tmp_path):
        """Test that execute() method calls ensure_ripgrep()."""
        # Setup mocks for all execute() dependencies
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch.object(executor, "_calculate_auto_update_hash", return_value="same_hash"):
            with patch.object(executor, "git_pull", return_value=True):
                with patch.object(executor, "git_submodule_update", return_value=True):
                    with patch.object(executor, "build_custom_hnswlib", return_value=True):
                        with patch.object(executor, "pip_install", return_value=True):
                            with patch.object(executor, "ensure_ripgrep") as mock_ensure_rg:
                                mock_ensure_rg.return_value = True

                                result = executor.execute()

        # Verify ensure_ripgrep was called
        mock_ensure_rg.assert_called_once()

        # execute() returns True if all steps succeed
        assert result is True

    def test_execute_continues_if_ripgrep_fails(self, executor, tmp_path):
        """Test that execute() continues even if ensure_ripgrep fails."""
        # Setup mocks for all execute() dependencies
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch.object(executor, "_calculate_auto_update_hash", return_value="same_hash"):
            with patch.object(executor, "git_pull", return_value=True):
                with patch.object(executor, "git_submodule_update", return_value=True):
                    with patch.object(executor, "build_custom_hnswlib", return_value=True):
                        with patch.object(executor, "pip_install", return_value=True):
                            with patch.object(executor, "ensure_ripgrep") as mock_ensure_rg:
                                mock_ensure_rg.return_value = False  # ripgrep fails

                                result = executor.execute()

        # Verify ensure_ripgrep was called
        mock_ensure_rg.assert_called_once()

        # execute() should still return True (ripgrep is optional)
        assert result is True

    def test_execute_logs_ripgrep_success(self, executor, tmp_path, caplog):
        """Test that execute() logs INFO when ensure_ripgrep succeeds (Bug #157)."""
        import logging

        caplog.set_level(logging.INFO)

        # Setup mocks for all execute() dependencies
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch.object(executor, "_calculate_auto_update_hash", return_value="same_hash"):
            with patch.object(executor, "git_pull", return_value=True):
                with patch.object(executor, "git_submodule_update", return_value=True):
                    with patch.object(executor, "build_custom_hnswlib", return_value=True):
                        with patch.object(executor, "pip_install", return_value=True):
                            with patch.object(executor, "ensure_ripgrep") as mock_ensure_rg:
                                mock_ensure_rg.return_value = True

                                result = executor.execute()

        # Verify ripgrep success was logged at INFO level
        assert any(
            "ripgrep" in record.message.lower() and record.levelname == "INFO"
            for record in caplog.records
        ), "Expected INFO log for ripgrep success"
        assert result is True

    def test_execute_logs_ripgrep_failure(self, executor, tmp_path, caplog):
        """Test that execute() logs ERROR when ensure_ripgrep fails (Bug #157)."""
        import logging

        caplog.set_level(logging.ERROR)

        # Setup mocks for all execute() dependencies
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        with patch.object(executor, "_calculate_auto_update_hash", return_value="same_hash"):
            with patch.object(executor, "git_pull", return_value=True):
                with patch.object(executor, "git_submodule_update", return_value=True):
                    with patch.object(executor, "build_custom_hnswlib", return_value=True):
                        with patch.object(executor, "pip_install", return_value=True):
                            with patch.object(executor, "ensure_ripgrep") as mock_ensure_rg:
                                mock_ensure_rg.return_value = False

                                result = executor.execute()

        # Verify ripgrep failure was logged at ERROR level with error code
        assert any(
            "ripgrep" in record.message.lower() and record.levelname == "ERROR"
            for record in caplog.records
        ), "Expected ERROR log for ripgrep failure"
        assert result is True  # Still returns True (ripgrep is optional)
