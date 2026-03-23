"""Unit tests for NfsHealthMonitor.

Strategy: inject a mock NfsMountValidator so tests never touch real NFS or
the filesystem beyond what pytest tmp_path provides.  We treat the validator
as a dependency and control its output directly — this is minimal, justified
mocking (the validator has its own test suite).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock


from code_indexer.server.storage.shared.nfs_health_monitor import NfsHealthMonitor
from code_indexer.server.storage.shared.nfs_validator import NfsMountValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _healthy_result(mount: str = "/mnt/fsx") -> dict:
    return {
        "healthy": True,
        "mount_point": mount,
        "writable": True,
        "latency_ms": 1.5,
        "error": None,
    }


def _unhealthy_result(mount: str = "/mnt/fsx", error: str = "NFS gone") -> dict:
    return {
        "healthy": False,
        "mount_point": mount,
        "writable": False,
        "latency_ms": 0.0,
        "error": error,
    }


def _make_mock_validator(result: dict, mount: str = "/mnt/fsx") -> MagicMock:
    mock = MagicMock(spec=NfsMountValidator)
    mock._mount_point = Path(mount)
    mock.validate.return_value = result
    return mock


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_initial_healthy_true(self) -> None:
        """Before start(), is_healthy defaults to True (optimistic)."""
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator)
        assert monitor.is_healthy is True

    def test_initial_last_check_empty(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator)
        assert monitor.get_last_check() == {}

    def test_custom_check_interval_stored(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        assert monitor._check_interval == 60


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_spawns_thread(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            assert monitor._thread is not None
            assert monitor._thread.is_alive()
        finally:
            monitor.stop()

    def test_stop_terminates_thread(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        monitor.start()
        monitor.stop()
        assert monitor._thread is None

    def test_start_runs_initial_check(self) -> None:
        """start() performs an immediate check before spawning the thread."""
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            assert validator.validate.called
        finally:
            monitor.stop()

    def test_double_start_does_not_raise(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            monitor.start()  # Should log a warning, not raise
        finally:
            monitor.stop()

    def test_stop_without_start_does_not_raise(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        monitor.stop()  # Should not raise


# ---------------------------------------------------------------------------
# is_healthy property
# ---------------------------------------------------------------------------


class TestIsHealthy:
    def test_healthy_after_healthy_check(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            assert monitor.is_healthy is True
        finally:
            monitor.stop()

    def test_unhealthy_after_unhealthy_check(self) -> None:
        validator = _make_mock_validator(_unhealthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            assert monitor.is_healthy is False
        finally:
            monitor.stop()

    def test_health_updates_after_background_check(self) -> None:
        """After the interval elapses the background thread re-checks."""
        # Start healthy, then flip to unhealthy on second call
        validator = _make_mock_validator(_healthy_result())
        validator.validate.side_effect = [
            _healthy_result(),
            _unhealthy_result(error="NFS timeout"),
        ]
        monitor = NfsHealthMonitor(validator, check_interval=1)
        try:
            monitor.start()
            assert monitor.is_healthy is True  # Initial check → healthy
            # Wait for background thread to fire at least once
            deadline = time.monotonic() + 3.0
            while monitor.is_healthy and time.monotonic() < deadline:
                time.sleep(0.05)
            assert monitor.is_healthy is False, "Expected health to flip to False"
        finally:
            monitor.stop()


# ---------------------------------------------------------------------------
# get_last_check()
# ---------------------------------------------------------------------------


class TestGetLastCheck:
    def test_last_check_populated_after_start(self) -> None:
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            last = monitor.get_last_check()
            assert "healthy" in last
            assert "checked_at" in last
        finally:
            monitor.stop()

    def test_last_check_reflects_healthy_result(self) -> None:
        validator = _make_mock_validator(_healthy_result(mount="/mnt/fsx"))
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            last = monitor.get_last_check()
            assert last["healthy"] is True
            assert last["mount_point"] == "/mnt/fsx"
        finally:
            monitor.stop()

    def test_last_check_reflects_unhealthy_result(self) -> None:
        validator = _make_mock_validator(_unhealthy_result(error="timeout"))
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            last = monitor.get_last_check()
            assert last["healthy"] is False
            assert last["error"] == "timeout"
        finally:
            monitor.stop()

    def test_get_last_check_returns_copy(self) -> None:
        """Mutating the returned dict must not affect internal state."""
        validator = _make_mock_validator(_healthy_result())
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            last = monitor.get_last_check()
            last["healthy"] = False  # Mutate the copy
            assert monitor.is_healthy is True  # Internal state unchanged
        finally:
            monitor.stop()


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_validator_exception_marks_unhealthy(self) -> None:
        """If validate() raises, monitor marks itself unhealthy without crashing."""
        validator = _make_mock_validator(_healthy_result())
        validator.validate.side_effect = RuntimeError("unexpected NFS error")
        monitor = NfsHealthMonitor(validator, check_interval=60)
        try:
            monitor.start()
            assert monitor.is_healthy is False
            last = monitor.get_last_check()
            assert "Unexpected exception" in last["error"]
        finally:
            monitor.stop()
