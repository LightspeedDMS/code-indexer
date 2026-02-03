"""
Unit tests for ripgrep installation in DeploymentExecutor.

Tests that DeploymentExecutor.ensure_ripgrep() properly delegates to the shared
RipgrepInstaller utility class. The actual ripgrep installation logic is
comprehensively tested in tests/unit/server/utils/test_ripgrep_installer.py.
"""

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

        # Verify RipgrepInstaller was instantiated (uses default Path.home())
        mock_ripgrep_class.assert_called_once_with()

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

        with patch.object(executor, "git_pull", return_value=True):
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

        with patch.object(executor, "git_pull", return_value=True):
            with patch.object(executor, "pip_install", return_value=True):
                with patch.object(executor, "ensure_ripgrep") as mock_ensure_rg:
                    mock_ensure_rg.return_value = False  # ripgrep fails

                    result = executor.execute()

        # Verify ensure_ripgrep was called
        mock_ensure_rg.assert_called_once()

        # execute() should still return True (ripgrep is optional)
        assert result is True
