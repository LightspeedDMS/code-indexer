"""
Tests for git submodule safe.directory configuration in auto-update.

Tests for DeploymentExecutor._ensure_submodule_safe_directory() method that adds
submodule paths to git's safe.directory configuration to avoid "dubious ownership"
errors when git tries to operate on submodules owned by a different user.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureSubmoduleSafeDirectory:
    """Tests for _ensure_submodule_safe_directory method."""

    def test_ensure_submodule_safe_directory_method_exists(self):
        """DeploymentExecutor should have _ensure_submodule_safe_directory method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_submodule_safe_directory")
        assert callable(getattr(executor, "_ensure_submodule_safe_directory"))

    def test_ensure_submodule_safe_directory_returns_true_when_submodule_not_exists(
        self,
    ):
        """Should return True when submodule directory doesn't exist yet."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/nonexistent"))

        # Submodule path won't exist
        result = executor._ensure_submodule_safe_directory()

        assert result is True

    def test_ensure_submodule_safe_directory_adds_hnswlib_path(self):
        """Should add third_party/hnswlib to git safe.directory."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run"
            ) as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                result = executor._ensure_submodule_safe_directory()

        assert result is True
        # Verify git config --add was called with submodule path
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "config" in call_args
        assert "--global" in call_args
        assert "--add" in call_args
        assert "safe.directory" in call_args
        assert "/home/user/code-indexer/third_party/hnswlib" in call_args[-1]

    def test_ensure_submodule_safe_directory_handles_git_config_failure(self):
        """Should return True even when git config fails (non-fatal)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run"
            ) as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1, stderr="fatal: unable to write"
                )

                result = executor._ensure_submodule_safe_directory()

        # Should still return True (non-fatal, continue with deployment)
        assert result is True

    def test_ensure_submodule_safe_directory_handles_exception(self):
        """Should return True and log warning on exception (non-fatal)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(Path, "exists", side_effect=Exception("Disk failure")):
            result = executor._ensure_submodule_safe_directory()

        # Should return True (non-fatal error)
        assert result is True

    def test_git_submodule_update_calls_ensure_submodule_safe_directory(self):
        """git_submodule_update should call _ensure_submodule_safe_directory first."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(
            executor, "_ensure_submodule_safe_directory", return_value=True
        ) as mock_ensure:
            with patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run"
            ) as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                executor.git_submodule_update()

        # Verify _ensure_submodule_safe_directory was called
        mock_ensure.assert_called_once()
