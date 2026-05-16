"""Tests for DeploymentExecutor._ensure_claude_cli_installed().

Verifies:
  AC1: claude already in PATH -> skips installer subprocess, returns True
  AC2: claude not found -> runs curl installer, returns True on success
  AC3: installer fails (nonzero) -> logs WARNING, returns False, no crash
  AC4: installer raises TimeoutExpired -> logs WARNING, returns False
  AC5: idempotent: two consecutive calls both return True when installed

Only true external dependencies are mocked (shutil.which, subprocess.run).
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor():
    """Minimal DeploymentExecutor for unit testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_warning_logged(caplog) -> None:
    """Assert that at least one WARNING-or-higher record was captured."""
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected at least one WARNING log record"
    )


# ---------------------------------------------------------------------------
# AC1: already installed -> skip subprocess, return True
# ---------------------------------------------------------------------------


class TestClaudeCliAlreadyInstalled:
    def test_returns_true_without_subprocess(self, executor):
        """shutil.which finds claude -> returns True, subprocess never called."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            result = executor._ensure_claude_cli_installed()

        assert result is True
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# AC2: not found -> runs installer, returns True on success
# ---------------------------------------------------------------------------


class TestClaudeCliNotFound:
    def test_calls_subprocess_and_returns_true(self, executor):
        """shutil.which returns None -> subprocess.run called once, returns True."""
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = executor._ensure_claude_cli_installed()

        assert result is True
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# AC3: installer fails (nonzero) -> WARNING, return False, no raise
# ---------------------------------------------------------------------------


class TestClaudeCliInstallerFails:
    def test_nonzero_returncode_returns_false_with_warning(self, executor, caplog):
        """returncode != 0 -> returns False and emits WARNING; must not raise."""
        fail_proc = MagicMock(returncode=1, stdout="", stderr="error")
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", return_value=fail_proc),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_claude_cli_installed()

        assert result is False
        _assert_warning_logged(caplog)


# ---------------------------------------------------------------------------
# AC4: TimeoutExpired -> WARNING, return False, no re-raise
# ---------------------------------------------------------------------------


class TestClaudeCliTimeout:
    def test_timeout_returns_false_with_warning(self, executor, caplog):
        """TimeoutExpired -> returns False, emits WARNING, must not propagate."""
        with (
            patch("shutil.which", return_value=None),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="sh", timeout=60),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = executor._ensure_claude_cli_installed()

        assert result is False
        _assert_warning_logged(caplog)


# ---------------------------------------------------------------------------
# AC5: idempotent
# ---------------------------------------------------------------------------


def test_idempotent_both_calls_return_true(executor):
    """Two calls in a row when claude is already present must both return True."""
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        assert executor._ensure_claude_cli_installed() is True
        assert executor._ensure_claude_cli_installed() is True
