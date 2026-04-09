"""
Unit tests for Story #355: Signal-Based Server Restart via Auto-Updater.

Tests for restart signal detection in AutoUpdateService.poll_once() (service.py).

TDD: These tests are written FIRST, before implementation.

Covers:
- Scenario 2: Auto-updater detects signal and restarts server
- Scenario 3: Signal file deleted even if restart fails
- Scenario 4: Stale signal file cleaned up (no restart triggered)
- Signal detected BEFORE PENDING_REDEPLOY_MARKER check
- Normal flow continues when no signal file present
- RESTART_SIGNAL_STALENESS_THRESHOLD constant exists
"""

import datetime
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch


@pytest.fixture
def service():
    """Create AutoUpdateService instance for testing."""
    from code_indexer.server.auto_update.service import AutoUpdateService

    svc = AutoUpdateService(
        repo_path=Path("/test/repo"),
        check_interval=60,
    )
    # Inject mock components
    svc.change_detector = Mock()
    svc.deployment_lock = Mock()
    svc.deployment_executor = Mock()
    return svc


def make_signal_file(path: Path, age_seconds: float = 10.0) -> None:
    """Helper: write a restart.signal file with a timestamp `age_seconds` old."""
    ts = datetime.datetime.now() - datetime.timedelta(seconds=age_seconds)
    data = {
        "timestamp": ts.isoformat(),
        "reason": "diagnostics_restart",
    }
    path.write_text(json.dumps(data))


class TestRestartSignalDetected:
    """Tests for Scenario 2: Auto-updater detects signal and restarts server."""

    def test_signal_file_triggers_restart_server_call(self, service, tmp_path):
        """
        Scenario 2: When signal file exists, restart_server() is called.

        Given the auto-updater polling loop is running
        And a restart.signal file exists at ~/.cidx-server/
        When the auto-updater executes its next poll cycle
        Then systemctl restart cidx-server is executed (via restart_server())
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        service.deployment_executor.restart_server.assert_called_once()

    def test_signal_file_deleted_before_restart_attempt(self, service, tmp_path):
        """
        Scenario 2: Signal file is deleted BEFORE restart attempt.

        Given a restart.signal file exists
        When the auto-updater detects it
        Then the signal file is deleted immediately (before restart attempt)
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        deleted_before_restart = []

        _original_restart = service.deployment_executor.restart_server

        def check_deleted_on_restart():
            # At time of restart, file should already be deleted
            deleted_before_restart.append(not signal_file.exists())
            return True

        service.deployment_executor.restart_server.side_effect = (
            check_deleted_on_restart
        )

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        assert deleted_before_restart == [True], (
            "Signal file must be deleted BEFORE restart_server() is called"
        )

    def test_signal_file_is_deleted_after_detection(self, service, tmp_path):
        """
        Scenario 2: Signal file does not exist after poll cycle completes.

        Given a restart.signal file exists
        When the auto-updater detects it and processes it
        Then the signal file is gone after the poll cycle
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        assert not signal_file.exists(), "Signal file must be deleted after processing"

    def test_signal_detection_skips_normal_change_detection(self, service, tmp_path):
        """
        Scenario 2: When signal file detected, normal change detection is skipped.

        Given a restart.signal file exists
        When the auto-updater detects it
        Then normal change detection is skipped for this cycle
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        service.change_detector.has_changes.assert_not_called()

    def test_signal_detection_logs_detection(self, service, tmp_path):
        """
        Scenario 2: Signal detection is logged.

        Given a restart.signal file exists
        When the auto-updater detects it
        Then a log message is emitted
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    with patch(
                        "code_indexer.server.auto_update.service.logger"
                    ) as mock_logger:
                        service.poll_once()

        assert mock_logger.info.called
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any(
            "signal" in msg.lower() or "restart" in msg.lower() for msg in log_calls
        )


class TestRestartSignalDeletedOnFailure:
    """Tests for Scenario 3: Signal file deleted even if restart fails."""

    def test_signal_file_deleted_even_when_restart_fails(self, service, tmp_path):
        """
        Scenario 3: If restart_server() fails, signal file is still deleted.

        Given a restart.signal file exists
        When the auto-updater detects it and restart_server() fails
        Then the signal file is still deleted (no retry loop)
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        service.deployment_executor.restart_server.return_value = False  # Failure

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        assert not signal_file.exists(), (
            "Signal file must be deleted even on restart failure"
        )

    def test_restart_failure_is_logged(self, service, tmp_path):
        """
        Scenario 3: Restart failure is logged.

        Given a restart.signal file exists
        When restart_server() fails
        Then the failure is logged
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        service.deployment_executor.restart_server.return_value = False  # Failure

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    with patch(
                        "code_indexer.server.auto_update.service.logger"
                    ) as mock_logger:
                        service.poll_once()

        # Error should be logged
        assert mock_logger.error.called or mock_logger.warning.called

    def test_no_retry_loop_on_restart_failure(self, service, tmp_path):
        """
        Scenario 3: No retry loop - restart_server() is called at most once.

        Given a restart.signal file exists
        When restart_server() fails
        Then restart_server() is called exactly once (no retry)
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        service.deployment_executor.restart_server.return_value = False  # Failure

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        assert service.deployment_executor.restart_server.call_count == 1


class TestStaleSignalFileCleaned:
    """Tests for Scenario 4: Stale signal file cleaned up without restart."""

    def test_stale_signal_file_deleted_without_restart(self, service, tmp_path):
        """
        Scenario 4: Stale signal file is deleted without triggering restart.

        Given a restart.signal file exists from a previous crash/power-loss
        And the signal is older than RESTART_SIGNAL_STALENESS_THRESHOLD seconds
        When the auto-updater starts a new poll cycle
        Then the stale signal file is deleted
        And NO restart is triggered
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_STALENESS_THRESHOLD,
        )

        signal_file = tmp_path / "restart.signal"
        # Make it older than the threshold
        make_signal_file(
            signal_file, age_seconds=RESTART_SIGNAL_STALENESS_THRESHOLD + 30
        )

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        # File must be deleted
        assert not signal_file.exists(), "Stale signal file must be deleted"
        # No restart
        service.deployment_executor.restart_server.assert_not_called()

    def test_stale_signal_logs_warning(self, service, tmp_path):
        """
        Scenario 4: Stale signal file detection emits a warning log.

        Given a stale restart.signal file exists
        When the auto-updater detects it
        Then a warning is logged
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_STALENESS_THRESHOLD,
        )

        signal_file = tmp_path / "restart.signal"
        make_signal_file(
            signal_file, age_seconds=RESTART_SIGNAL_STALENESS_THRESHOLD + 30
        )

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    with patch(
                        "code_indexer.server.auto_update.service.logger"
                    ) as mock_logger:
                        service.poll_once()

        assert mock_logger.warning.called
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any(
            "stale" in msg.lower() or "age" in msg.lower() or "old" in msg.lower()
            for msg in warning_calls
        )

    def test_fresh_signal_is_not_stale(self, service, tmp_path):
        """
        Scenario 4 (negative): Fresh signal is NOT stale - restart IS triggered.

        Given a restart.signal file exists with a fresh timestamp (just written)
        And the signal is within RESTART_SIGNAL_STALENESS_THRESHOLD
        When the auto-updater detects it
        Then restart IS triggered (not treated as stale)
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)  # 5 seconds old - fresh

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        # Fresh signal - restart should be triggered
        service.deployment_executor.restart_server.assert_called_once()

    def test_signal_exactly_at_threshold_is_stale(self, service, tmp_path):
        """
        Scenario 4 (boundary): Signal exactly at threshold is treated as stale.

        Signal age >= RESTART_SIGNAL_STALENESS_THRESHOLD is stale.
        Signal age < RESTART_SIGNAL_STALENESS_THRESHOLD is fresh.
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_STALENESS_THRESHOLD,
        )

        signal_file = tmp_path / "restart.signal"
        # Make it exactly at threshold
        make_signal_file(signal_file, age_seconds=RESTART_SIGNAL_STALENESS_THRESHOLD)

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        # At threshold - treated as stale, no restart
        service.deployment_executor.restart_server.assert_not_called()
        assert not signal_file.exists()


class TestSignalCheckedBeforeRedeployMarker:
    """Tests that restart signal is checked BEFORE PENDING_REDEPLOY_MARKER."""

    def test_restart_signal_check_precedes_redeploy_marker_check(
        self, service, tmp_path
    ):
        """
        Restart signal check must happen BEFORE PENDING_REDEPLOY_MARKER check.

        Given a restart.signal file exists
        When poll_once() runs
        Then the restart signal is processed (and returns early) before marker check
        """
        signal_file = tmp_path / "restart.signal"
        make_signal_file(signal_file, age_seconds=5)

        call_order = []

        mock_signal_path = MagicMock()
        mock_signal_path.exists.side_effect = lambda: (
            call_order.append("signal_check") or True  # type: ignore[func-returns-value]
        )
        mock_signal_path.__truediv__ = signal_file.__truediv__

        # Make signal file accessible via real path for reading
        mock_signal_path.exists.return_value = True
        # We need the real file to be read, so use the real signal_file
        # but track when it's checked

        mock_pending = MagicMock()

        def pending_exists():
            call_order.append("pending_check")
            return False

        mock_pending.exists.side_effect = pending_exists
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.poll_once()

        # Pending marker check should NOT happen because signal triggered early return
        assert "pending_check" not in call_order, (
            "PENDING_REDEPLOY_MARKER should not be checked when restart signal is present"
        )

    def test_no_signal_falls_through_to_redeploy_marker_check(self, service, tmp_path):
        """
        When no restart signal exists, normal flow (redeploy marker check) proceeds.

        Given no restart.signal file exists
        When poll_once() runs
        Then the PENDING_REDEPLOY_MARKER check proceeds normally
        """
        signal_file = tmp_path / "nonexistent.signal"  # Does not exist

        mock_pending = MagicMock()
        mock_pending.exists.return_value = False
        mock_legacy = MagicMock()
        mock_legacy.exists.return_value = False

        with patch(
            "code_indexer.server.auto_update.service.RESTART_SIGNAL_PATH", signal_file
        ):
            with patch(
                "code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER",
                mock_pending,
            ):
                with patch(
                    "code_indexer.server.auto_update.service.LEGACY_REDEPLOY_MARKER",
                    mock_legacy,
                ):
                    service.change_detector.has_changes.return_value = False
                    service.poll_once()

        # No restart
        service.deployment_executor.restart_server.assert_not_called()
        # Normal change detection ran
        service.change_detector.has_changes.assert_called_once()


class TestSignalConstantsExist:
    """Tests that required constants are properly defined."""

    def test_restart_signal_staleness_threshold_constant_exists(self):
        """
        RESTART_SIGNAL_STALENESS_THRESHOLD constant must exist in deployment_executor.
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_STALENESS_THRESHOLD,
        )

        assert RESTART_SIGNAL_STALENESS_THRESHOLD is not None
        assert isinstance(RESTART_SIGNAL_STALENESS_THRESHOLD, int)

    def test_restart_signal_staleness_threshold_is_120_seconds(self):
        """
        RESTART_SIGNAL_STALENESS_THRESHOLD must be 120 seconds (2x poll interval).
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_STALENESS_THRESHOLD,
        )

        assert RESTART_SIGNAL_STALENESS_THRESHOLD == 120

    def test_restart_signal_path_constant_exists(self):
        """
        RESTART_SIGNAL_PATH constant must exist in deployment_executor.
        """
        from code_indexer.server.auto_update.deployment_executor import (
            RESTART_SIGNAL_PATH,
        )

        assert RESTART_SIGNAL_PATH == Path.home() / ".cidx-server" / "restart.signal"

    def test_service_imports_restart_signal_constants(self):
        """
        service.py must import RESTART_SIGNAL_PATH from deployment_executor.
        """
        import code_indexer.server.auto_update.service as service_module

        assert hasattr(service_module, "RESTART_SIGNAL_PATH"), (
            "service.py must have RESTART_SIGNAL_PATH attribute "
            "(imported from deployment_executor)"
        )
