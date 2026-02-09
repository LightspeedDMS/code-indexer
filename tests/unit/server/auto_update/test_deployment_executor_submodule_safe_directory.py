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

    def test_git_submodule_update_only_initializes_hnswlib(self):
        """git_submodule_update should only init third_party/hnswlib, not all submodules.

        Production servers don't need test fixtures. Using --recursive would try to
        initialize all submodules (including test-fixtures/*) which fail safe.directory
        checks.
        """
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run"
            ) as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                executor.git_submodule_update()

        # Verify git command uses specific path, not --recursive
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "submodule" in call_args
        assert "update" in call_args
        assert "--init" in call_args
        # Should NOT use --recursive (which inits ALL submodules)
        assert "--recursive" not in call_args
        # Should specify the specific submodule path
        assert "third_party/hnswlib" in call_args


class TestCleanupSubmoduleState:
    """Tests for _cleanup_submodule_state method."""

    def test_cleanup_submodule_state_method_exists(self):
        """DeploymentExecutor should have _cleanup_submodule_state method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_cleanup_submodule_state")
        assert callable(getattr(executor, "_cleanup_submodule_state"))

    def test_cleanup_submodule_state_removes_git_modules(self):
        """Should remove .git/modules/{submodule_path} directory."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = executor._cleanup_submodule_state("third_party/hnswlib")

        assert result is True
        # Should have called rm -rf twice (git modules + worktree)
        assert mock_run.call_count == 2

        # First call: remove .git/modules/third_party/hnswlib
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "sudo" in first_call_args
        assert "rm" in first_call_args
        assert "-rf" in first_call_args
        assert ".git/modules/third_party/hnswlib" in first_call_args[-1]

        # Second call: remove third_party/hnswlib worktree
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "sudo" in second_call_args
        assert "rm" in second_call_args
        assert "-rf" in second_call_args
        assert "third_party/hnswlib" in second_call_args[-1]

    def test_cleanup_submodule_state_handles_rm_failure(self):
        """Should return False when rm command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr="Permission denied"
            )

            result = executor._cleanup_submodule_state("third_party/hnswlib")

        assert result is False

    def test_cleanup_submodule_state_handles_exception(self):
        """Should return False and log error on exception."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.subprocess.run",
            side_effect=Exception("Subprocess error")
        ):
            result = executor._cleanup_submodule_state("third_party/hnswlib")

        assert result is False


class TestSubmoduleRetryLogic:
    """Tests for retry logic in git_submodule_update."""

    def test_git_submodule_update_succeeds_on_first_attempt(self):
        """Should return True on first successful attempt without retry."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.subprocess.run"
            ) as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")

                result = executor.git_submodule_update()

        assert result is True
        # Should only call once (no retry)
        assert mock_run.call_count == 1

    def test_git_submodule_update_retries_on_config_lock_error(self):
        """Should cleanup and retry when encountering config.lock error."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=True) as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    # First attempt fails with config.lock error
                    # Second attempt succeeds
                    mock_run.side_effect = [
                        MagicMock(
                            returncode=1,
                            stderr="Unable to create '.git/modules/third_party/hnswlib/config.lock'"
                        ),
                        MagicMock(returncode=0, stdout="success", stderr="")
                    ]

                    result = executor.git_submodule_update()

        assert result is True
        # Should call cleanup once
        mock_cleanup.assert_called_once_with("third_party/hnswlib")
        # Should call git submodule update twice (initial + retry)
        assert mock_run.call_count == 2

    def test_git_submodule_update_retries_on_already_exists_error(self):
        """Should cleanup and retry when encountering 'already exists' error."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=True) as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.side_effect = [
                        MagicMock(
                            returncode=1,
                            stderr="destination path 'third_party/hnswlib' already exists"
                        ),
                        MagicMock(returncode=0, stdout="success", stderr="")
                    ]

                    result = executor.git_submodule_update()

        assert result is True
        mock_cleanup.assert_called_once_with("third_party/hnswlib")
        assert mock_run.call_count == 2

    def test_git_submodule_update_retries_on_repository_handle_error(self):
        """Should cleanup and retry when encountering repository handle error."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=True) as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.side_effect = [
                        MagicMock(
                            returncode=1,
                            stderr="could not get a repository handle for submodule 'third_party/hnswlib'"
                        ),
                        MagicMock(returncode=0, stdout="success", stderr="")
                    ]

                    result = executor.git_submodule_update()

        assert result is True
        mock_cleanup.assert_called_once_with("third_party/hnswlib")
        assert mock_run.call_count == 2

    def test_git_submodule_update_retries_on_worktree_error(self):
        """Should cleanup and retry when encountering worktree error."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=True) as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.side_effect = [
                        MagicMock(
                            returncode=1,
                            stderr="worktree configuration error"
                        ),
                        MagicMock(returncode=0, stdout="success", stderr="")
                    ]

                    result = executor.git_submodule_update()

        assert result is True
        mock_cleanup.assert_called_once_with("third_party/hnswlib")
        assert mock_run.call_count == 2

    def test_git_submodule_update_no_retry_on_network_error(self):
        """Should NOT retry on network errors (non-recoverable)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state") as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="Could not resolve host: github.com"
                    )

                    result = executor.git_submodule_update()

        assert result is False
        # Should NOT call cleanup on network error
        mock_cleanup.assert_not_called()
        # Should only attempt once (no retry)
        assert mock_run.call_count == 1

    def test_git_submodule_update_no_retry_on_auth_error(self):
        """Should NOT retry on authentication errors (non-recoverable)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state") as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="Authentication failed"
                    )

                    result = executor.git_submodule_update()

        assert result is False
        mock_cleanup.assert_not_called()
        assert mock_run.call_count == 1

    def test_git_submodule_update_no_retry_on_unable_to_access_error(self):
        """Should NOT retry on unable to access errors (non-recoverable)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state") as mock_cleanup:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="fatal: unable to access 'https://github.com/repo.git/'"
                    )

                    result = executor.git_submodule_update()

        assert result is False
        mock_cleanup.assert_not_called()
        assert mock_run.call_count == 1

    def test_git_submodule_update_fails_when_cleanup_fails(self):
        """Should return False when cleanup fails on recoverable error."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=False):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=1,
                        stderr="config.lock error"
                    )

                    result = executor.git_submodule_update()

        assert result is False
        # Should only attempt once (cleanup failed, so no retry)
        assert mock_run.call_count == 1

    def test_git_submodule_update_fails_when_retry_also_fails(self):
        """Should return False when both initial attempt and retry fail."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
        )

        with patch.object(executor, "_ensure_submodule_safe_directory", return_value=True):
            with patch.object(executor, "_cleanup_submodule_state", return_value=True):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.subprocess.run"
                ) as mock_run:
                    # Both attempts fail
                    mock_run.side_effect = [
                        MagicMock(returncode=1, stderr="config.lock error"),
                        MagicMock(returncode=1, stderr="still failing")
                    ]

                    result = executor.git_submodule_update()

        assert result is False
        # Should call twice (initial + retry)
        assert mock_run.call_count == 2
