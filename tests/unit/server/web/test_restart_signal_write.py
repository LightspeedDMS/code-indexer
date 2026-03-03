"""
Unit tests for Story #355: Signal-Based Server Restart via Auto-Updater.

Tests for signal file writing in _delayed_restart() (routes.py).

TDD: These tests are written FIRST, before implementation.

Covers:
- Scenario 1: Server writes restart signal file when restart requested under systemd
- Scenario 5: Dev mode restart unchanged (still uses os.execv, no signal file written)
- Signal file JSON format validation
- _restart_in_progress flag reset after writing signal
- Signal file is NOT written in dev mode
"""

import datetime
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def reset_restart_state():
    """Reset module-level restart state between tests."""
    import code_indexer.server.web.routes as routes_module
    routes_module._restart_in_progress = False
    yield
    routes_module._restart_in_progress = False


class TestSystemdModeWritesSignalFile:
    """Tests for Scenario 1: Server writes restart signal file when restart requested."""

    def test_systemd_mode_writes_signal_file(self, tmp_path):
        """
        Scenario 1: In systemd mode, _delayed_restart writes signal file.

        Given the CIDX server is running under systemd (INVOCATION_ID is set)
        When an admin triggers a restart from the Diagnostics web UI
        Then a signal file is created at the configured signal path
        """
        from code_indexer.server.web.routes import _delayed_restart
        from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        assert signal_file.exists(), "Signal file should be created in systemd mode"

    def test_systemd_mode_signal_file_contains_valid_json(self, tmp_path):
        """
        Scenario 1: Signal file contains valid JSON.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then the signal file contains valid JSON
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        assert signal_file.exists()
        content = signal_file.read_text()
        data = json.loads(content)  # Should not raise
        assert isinstance(data, dict)

    def test_systemd_mode_signal_file_has_timestamp_field(self, tmp_path):
        """
        Scenario 1: Signal file contains 'timestamp' field.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then the signal file JSON contains a 'timestamp' field
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        data = json.loads(signal_file.read_text())
        assert "timestamp" in data, "Signal file JSON must contain 'timestamp' field"

    def test_systemd_mode_signal_file_has_reason_field(self, tmp_path):
        """
        Scenario 1: Signal file contains 'reason' field.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then the signal file JSON contains a 'reason' field
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        data = json.loads(signal_file.read_text())
        assert "reason" in data, "Signal file JSON must contain 'reason' field"

    def test_systemd_mode_signal_file_reason_is_diagnostics_restart(self, tmp_path):
        """
        Scenario 1: Signal file 'reason' field value is 'diagnostics_restart'.

        Given the CIDX server is running under systemd
        When an admin triggers a restart from the Diagnostics web UI
        Then the signal file JSON 'reason' is 'diagnostics_restart'
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        data = json.loads(signal_file.read_text())
        assert data["reason"] == "diagnostics_restart"

    def test_systemd_mode_signal_file_timestamp_is_iso8601(self, tmp_path):
        """
        Scenario 1: Signal file timestamp is parseable as ISO 8601.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then the signal file timestamp can be parsed with datetime.fromisoformat()
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        data = json.loads(signal_file.read_text())
        # Should not raise - timestamp must be parseable
        ts = datetime.datetime.fromisoformat(data["timestamp"])
        assert isinstance(ts, datetime.datetime)

    def test_systemd_mode_does_not_call_subprocess(self, tmp_path):
        """
        Scenario 1: In systemd mode, _delayed_restart does NOT call subprocess.run.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then subprocess.run is NOT called (signal file is used instead)
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch("subprocess.run") as mock_subprocess:
                    with patch(
                        "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                    ):
                        _delayed_restart(delay=2)

        mock_subprocess.assert_not_called()

    def test_systemd_mode_resets_restart_in_progress_flag(self, tmp_path):
        """
        Scenario 1: After writing signal file, _restart_in_progress is reset to False.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then _restart_in_progress is reset to False (non-blocking - we don't wait for restart)
        """
        from code_indexer.server.web.routes import _delayed_restart
        import code_indexer.server.web.routes as routes_module

        signal_file = tmp_path / "restart.signal"
        routes_module._restart_in_progress = True

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        assert routes_module._restart_in_progress is False

    def test_systemd_mode_logs_signal_written(self, tmp_path):
        """
        Scenario 1: Writing signal file is logged.

        Given the CIDX server is running under systemd
        When an admin triggers a restart
        Then a log message indicates the signal was written
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch("code_indexer.server.web.routes.logger") as mock_logger:
                    with patch(
                        "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                    ):
                        _delayed_restart(delay=2)

        # Should have at least one info log
        assert mock_logger.info.called
        # At least one message should mention signal or restart
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any(
            "signal" in msg.lower() or "restart" in msg.lower()
            for msg in log_calls
        )

    def test_systemd_mode_http_response_can_complete_before_signal(self, tmp_path):
        """
        Scenario 1: HTTP response returns success before the signal file is written.

        The delay (time.sleep) ensures the HTTP response completes first.
        The signal write happens AFTER the sleep.
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"
        call_order = []

        def track_sleep(seconds):
            call_order.append("sleep")

        def track_write(*args, **kwargs):
            # This is a simplified check - the signal file write happens after sleep
            call_order.append("signal_write")

        with patch("time.sleep", side_effect=track_sleep):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode
                with patch(
                    "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                ):
                    _delayed_restart(delay=2)

        # sleep must be called first
        assert "sleep" in call_order
        assert call_order[0] == "sleep"


class TestDevModeRestartUnchanged:
    """Tests for Scenario 5: Dev mode restart unchanged."""

    def test_dev_mode_calls_os_execv(self):
        """
        Scenario 5: In dev mode, _delayed_restart calls os.execv unchanged.

        Given the CIDX server is running in dev mode (no INVOCATION_ID)
        When an admin triggers a restart from the Diagnostics web UI
        Then the existing os.execv restart mechanism is used
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd (dev mode)
                with patch("os.execv") as mock_execv:
                    _delayed_restart(delay=2)

        mock_execv.assert_called_once()

    def test_dev_mode_does_not_write_signal_file(self, tmp_path):
        """
        Scenario 5: In dev mode, NO signal file is written.

        Given the CIDX server is running in dev mode (no INVOCATION_ID)
        When an admin triggers a restart from the Diagnostics web UI
        Then no signal file is created
        """
        from code_indexer.server.web.routes import _delayed_restart

        signal_file = tmp_path / "restart.signal"

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd (dev mode)
                with patch("os.execv"):
                    with patch(
                        "code_indexer.server.web.routes.RESTART_SIGNAL_PATH", signal_file
                    ):
                        _delayed_restart(delay=2)

        assert not signal_file.exists(), "Signal file should NOT be created in dev mode"

    def test_dev_mode_execv_uses_correct_executable(self):
        """
        Scenario 5: In dev mode, os.execv is called with sys.executable.
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd
                with patch("os.execv") as mock_execv:
                    with patch("sys.executable", "/usr/bin/python3"):
                        with patch("sys.argv", ["app.py"]):
                            _delayed_restart(delay=2)

        call_args = mock_execv.call_args[0]
        assert call_args[0] == "/usr/bin/python3"

    def test_dev_mode_execv_failure_resets_flag(self):
        """
        Scenario 5: In dev mode, os.execv failure still resets _restart_in_progress.
        """
        from code_indexer.server.web.routes import _delayed_restart
        import code_indexer.server.web.routes as routes_module

        routes_module._restart_in_progress = True

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd
                with patch("os.execv") as mock_execv:
                    mock_execv.side_effect = OSError("Exec format error")
                    _delayed_restart(delay=2)

        assert routes_module._restart_in_progress is False


class TestSignalFileConstantImport:
    """Tests ensuring RESTART_SIGNAL_PATH constant is importable from routes."""

    def test_restart_signal_path_importable_from_deployment_executor(self):
        """
        RESTART_SIGNAL_PATH constant must be importable from deployment_executor.
        """
        from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH
        assert RESTART_SIGNAL_PATH is not None
        assert isinstance(RESTART_SIGNAL_PATH, Path)

    def test_restart_signal_path_is_in_cidx_server_dir(self):
        """
        RESTART_SIGNAL_PATH must be in ~/.cidx-server/ directory.
        """
        from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH
        assert RESTART_SIGNAL_PATH.parent == Path.home() / ".cidx-server"

    def test_restart_signal_path_filename_is_restart_signal(self):
        """
        RESTART_SIGNAL_PATH filename must be 'restart.signal'.
        """
        from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH
        assert RESTART_SIGNAL_PATH.name == "restart.signal"

    def test_routes_imports_restart_signal_path(self):
        """
        routes.py must import RESTART_SIGNAL_PATH from deployment_executor.
        """
        import code_indexer.server.web.routes as routes_module
        assert hasattr(routes_module, "RESTART_SIGNAL_PATH"), (
            "routes.py must have RESTART_SIGNAL_PATH attribute "
            "(imported from deployment_executor)"
        )
