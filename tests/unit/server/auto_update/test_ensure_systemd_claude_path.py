"""Tests for DeploymentExecutor._ensure_systemd_claude_path().

Verifies:
  AC1: Service file not found -> logs WARNING, returns False (non-fatal)
  AC2: PATH already contains ~/.local/bin -> skip, return True (idempotent, no subprocess)
  AC3: PATH line exists but missing ~/.local/bin -> replace in-place with prepended version,
       write exact full file content, reload, return True
  AC4: No PATH Environment= line -> append full default PATH line at end of file,
       write exact full file content, reload, return True
  AC5: sudo tee fails (nonzero exit) -> logs WARNING, returns False
  AC6: sudo systemctl daemon-reload fails -> logs WARNING, returns False

Only true external dependencies are mocked:
  - subprocess.run (for sudo tee and systemctl daemon-reload)
  - Path.home (home directory resolution)
  - SYSTEMD_UNIT_DIR (service file location)
"""

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "cidx-server"
FAKE_HOME = Path("/home/testuser")
LOCAL_BIN = "/home/testuser/.local/bin"

PATH_WITHOUT_LOCAL_BIN = (
    'Environment="PATH=/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"'
)
PATH_WITH_LOCAL_BIN = (
    f'Environment="PATH={LOCAL_BIN}:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"'
)
DEFAULT_PATH_LINE = (
    f'Environment="PATH={LOCAL_BIN}:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"'
)

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor():
    """Minimal DeploymentExecutor for unit testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name=SERVICE_NAME,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _has_warning(caplog) -> bool:
    """Return True if caplog contains at least one WARNING-or-above record."""
    return any(r.levelno >= logging.WARNING for r in caplog.records)


def _run_with_file(executor, service_content, *, tee_rc=0, reload_rc=0):
    """Write service_content to a temp unit dir and run _ensure_systemd_claude_path.

    Returns (result, mock_run) so callers can inspect subprocess calls.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        unit_dir = Path(tmpdir)
        service_file = unit_dir / f"{SERVICE_NAME}.service"
        service_file.write_text(service_content)

        with patch(
            "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
            unit_dir,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.Path.home",
                return_value=FAKE_HOME,
            ):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=tee_rc),
                        MagicMock(returncode=reload_rc),
                    ]
                    result = executor._ensure_systemd_claude_path()
                    return result, mock_run


def _run_without_file(executor):
    """Run _ensure_systemd_claude_path when the service file does not exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        unit_dir = Path(tmpdir)
        with patch(
            "code_indexer.server.auto_update.deployment_executor.SYSTEMD_UNIT_DIR",
            unit_dir,
        ):
            with patch(
                "code_indexer.server.auto_update.deployment_executor.Path.home",
                return_value=FAKE_HOME,
            ):
                return executor._ensure_systemd_claude_path()


def _tee_payload(mock_run) -> str:
    """Return the stdin string passed to the first subprocess.run (sudo tee) call."""
    first_call = mock_run.call_args_list[0]
    return str(first_call.kwargs.get("input", ""))


# ---------------------------------------------------------------------------
# AC1: Service file not found -> returns False, logs WARNING
# ---------------------------------------------------------------------------


class TestServiceFileNotFound:
    def test_returns_false_when_service_file_missing(self, executor):
        assert _run_without_file(executor) is False

    def test_warning_logged_when_service_file_missing(self, executor, caplog):
        with caplog.at_level(logging.WARNING):
            _run_without_file(executor)
        assert _has_warning(caplog)


# ---------------------------------------------------------------------------
# AC2: PATH already contains ~/.local/bin -> skip, return True, no subprocess
# ---------------------------------------------------------------------------


class TestPathAlreadyContainsLocalBin:
    def test_returns_true_without_subprocess(self, executor):
        service_content = f"[Service]\n{PATH_WITH_LOCAL_BIN}\nExecStart=/bin/app\n"
        result, mock_run = _run_with_file(executor, service_content)

        assert result is True
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# AC3: PATH line exists but missing ~/.local/bin -> replace in-place
# ---------------------------------------------------------------------------


class TestPathLineMissingLocalBin:
    # Exact input and its exact expected output after in-place replacement
    _INPUT = f"[Service]\n{PATH_WITHOUT_LOCAL_BIN}\nExecStart=/bin/app\n"
    _EXPECTED = f"[Service]\n{PATH_WITH_LOCAL_BIN}\nExecStart=/bin/app\n"

    def test_returns_true_after_prepend(self, executor):
        result, _ = _run_with_file(executor, self._INPUT)
        assert result is True

    def test_written_content_equals_expected_file(self, executor):
        """Tee payload must be the exact full service file with PATH replaced in-place."""
        result, mock_run = _run_with_file(executor, self._INPUT)

        assert result is True
        payload = _tee_payload(mock_run)
        assert payload == self._EXPECTED, (
            f"Expected:\n{self._EXPECTED!r}\nGot:\n{payload!r}"
        )

    def test_daemon_reload_called_after_write(self, executor):
        result, mock_run = _run_with_file(executor, self._INPUT)

        assert result is True
        assert mock_run.call_count == 2
        reload_cmd = mock_run.call_args_list[1][0][0]
        assert any("daemon-reload" in str(a) for a in reload_cmd)


# ---------------------------------------------------------------------------
# AC4: No PATH Environment= line -> append default PATH line at end of file
# ---------------------------------------------------------------------------


class TestNoPathLineAddsDefault:
    _INPUT = "[Service]\nExecStart=/bin/app\n"
    # Expected: PATH line appended at end of existing file content
    _EXPECTED = f"[Service]\nExecStart=/bin/app\n{DEFAULT_PATH_LINE}\n"

    def test_returns_true_when_no_path_line(self, executor):
        result, _ = _run_with_file(executor, self._INPUT)
        assert result is True

    def test_written_content_equals_expected_file(self, executor):
        """Tee payload must be the exact full service file with PATH line appended at end."""
        result, mock_run = _run_with_file(executor, self._INPUT)

        assert result is True
        payload = _tee_payload(mock_run)
        assert payload == self._EXPECTED, (
            f"Expected:\n{self._EXPECTED!r}\nGot:\n{payload!r}"
        )

    def test_daemon_reload_called_when_no_path_line(self, executor):
        result, mock_run = _run_with_file(executor, self._INPUT)

        assert result is True
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# AC5: sudo tee fails -> returns False, logs WARNING
# ---------------------------------------------------------------------------


class TestSudoTeeFails:
    _INPUT = f"[Service]\n{PATH_WITHOUT_LOCAL_BIN}\n"

    def test_returns_false_on_tee_failure(self, executor):
        result, _ = _run_with_file(executor, self._INPUT, tee_rc=1)
        assert result is False

    def test_warning_logged_on_tee_failure(self, executor, caplog):
        with caplog.at_level(logging.WARNING):
            _run_with_file(executor, self._INPUT, tee_rc=1)
        assert _has_warning(caplog)


# ---------------------------------------------------------------------------
# AC6: sudo systemctl daemon-reload fails -> returns False, logs WARNING
# ---------------------------------------------------------------------------


class TestDaemonReloadFails:
    _INPUT = f"[Service]\n{PATH_WITHOUT_LOCAL_BIN}\n"

    def test_returns_false_on_reload_failure(self, executor):
        result, _ = _run_with_file(executor, self._INPUT, reload_rc=1)
        assert result is False

    def test_warning_logged_on_reload_failure(self, executor, caplog):
        with caplog.at_level(logging.WARNING):
            _run_with_file(executor, self._INPUT, reload_rc=1)
        assert _has_warning(caplog)
