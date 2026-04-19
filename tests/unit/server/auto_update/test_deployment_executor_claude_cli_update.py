"""
Tests for Claude CLI auto-update in deployment executor.

Tests for DeploymentExecutor._ensure_claude_cli_updated() method that runs
`npm install -g @anthropic-ai/claude-code@latest` during every auto-deploy
so production servers stay on current Claude CLI versions.

Bug #839: Production servers were pinned to stale Claude CLI (Opus 4.5 / 200K context)
because the initial install from installer.py was never refreshed.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.auto_update.deployment_executor import (
    CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS,
    DeploymentExecutor,
)


class TestEnsureClaudeCliUpdated:
    """Tests for _ensure_claude_cli_updated and execute() wiring."""

    def test_ensure_claude_cli_updated_invokes_npm_install_latest(self):
        """Should call npm install -g @anthropic-ai/claude-code@latest with timeout=180."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(executor, "_is_npm_available", return_value=True):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                executor._ensure_claude_cli_updated()

        mock_run.assert_called_once_with(
            ["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
            timeout=180,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_ensure_claude_cli_updated_returns_true_on_success(self):
        """Should return True when npm install completes successfully."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch.object(executor, "_is_npm_available", return_value=True):
            with patch("subprocess.run", return_value=mock_result):
                result = executor._ensure_claude_cli_updated()

        assert result is True

    def test_ensure_claude_cli_updated_returns_false_on_error(self):
        """Should return False and log ERROR when npm install raises CalledProcessError."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
            stderr="npm ERR! network error",
        )

        with patch.object(executor, "_is_npm_available", return_value=True):
            with patch("subprocess.run", side_effect=error):
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.logger"
                ) as mock_logger:
                    result = executor._ensure_claude_cli_updated()

        assert result is False
        error_calls = [str(c) for c in mock_logger.error.call_args_list]
        assert any("npm ERR! network error" in c for c in error_calls)

    def test_ensure_claude_cli_updated_returns_false_on_timeout(self):
        """Should return False and log ERROR mentioning 'timed out' on TimeoutExpired.

        subprocess.run is called twice: once for npm --version (succeeds) and once
        for npm install (raises TimeoutExpired). Only the external seam is patched.
        """
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        npm_version_ok = MagicMock(returncode=0)
        timeout_error = subprocess.TimeoutExpired(
            cmd=["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
            timeout=CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS,
        )

        with patch(
            "subprocess.run", side_effect=[npm_version_ok, timeout_error]
        ) as _mock_run:
            with patch(
                "code_indexer.server.auto_update.deployment_executor.logger"
            ) as mock_logger:
                result = executor._ensure_claude_cli_updated()

        assert result is False
        error_calls = [str(c) for c in mock_logger.error.call_args_list]
        assert any("timed out" in c for c in error_calls)

    def test_ensure_claude_cli_updated_handles_spawn_error(self):
        """Should return False (not raise) when npm install raises FileNotFoundError.

        subprocess.run is called twice: once for npm --version (succeeds) and once
        for npm install (raises FileNotFoundError). Guards the non-fatal contract.
        Only the external seam is patched.
        """
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        npm_version_ok = MagicMock(returncode=0)

        with patch(
            "subprocess.run",
            side_effect=[
                npm_version_ok,
                FileNotFoundError("npm disappeared mid-install"),
            ],
        ):
            result = executor._ensure_claude_cli_updated()

        assert result is False

    def test_ensure_claude_cli_updated_returns_false_when_npm_missing(self):
        """Should return False without calling subprocess and log WARNING when npm absent."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))

        with patch.object(executor, "_is_npm_available", return_value=False):
            with patch("subprocess.run") as mock_run:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.logger"
                ) as mock_logger:
                    result = executor._ensure_claude_cli_updated()

        assert result is False
        mock_run.assert_not_called()
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("npm" in c.lower() for c in warning_calls)

    def test_execute_calls_ensure_claude_cli_updated(self):
        """execute() must call _ensure_claude_cli_updated after workers_config and cidx_repo_root."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        call_order = []

        def record(name):
            def inner(*args, **kwargs):
                call_order.append(name)
                return True

            return inner

        with (
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(
                executor, "_ensure_workers_config", side_effect=record("workers_config")
            ),
            patch.object(
                executor, "_ensure_cidx_repo_root", side_effect=record("cidx_repo_root")
            ),
            patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_sudoers_restart", return_value=True),
            patch.object(executor, "_ensure_memory_overcommit", return_value=True),
            patch.object(executor, "_ensure_swap_file", return_value=True),
            patch.object(
                executor,
                "_ensure_claude_cli_updated",
                side_effect=record("claude_cli_updated"),
            ),
            patch.object(
                executor, "_calculate_auto_update_hash", return_value="abc123"
            ),
        ):
            executor.execute()

        assert "claude_cli_updated" in call_order
        idx_claude = call_order.index("claude_cli_updated")
        assert idx_claude > call_order.index("workers_config")
        assert idx_claude > call_order.index("cidx_repo_root")
