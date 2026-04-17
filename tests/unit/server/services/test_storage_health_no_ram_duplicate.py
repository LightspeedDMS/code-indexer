"""
Regression tests for Bug #737: Server status reports RAM usage twice
(duplicated under Storage).

The _check_storage_health() method must not examine RAM/memory.
Storage health is disk-only.  RAM is already reported by
_collect_resource_failures() via the "RAM: X%" failure reason.
"""

from unittest.mock import patch, MagicMock

import pytest

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo

# Named constants — no magic numbers in test bodies
HEALTHY_DISK_PERCENT = 30.0
WARNING_DISK_PERCENT = 85.0
HIGH_MEMORY_PERCENT = 85.0
CRITICAL_MEMORY_PERCENT = 88.0
NORMAL_MEMORY_PERCENT = 20.0
NORMAL_CPU_PERCENT = 20.0
LARGE_DISK_FREE_GB = 200.0
LOW_DISK_FREE_GB = 5.0


def _disk_usage(percent: float, free_gb: float = LARGE_DISK_FREE_GB) -> MagicMock:
    """Return a mock psutil disk_usage result."""
    mock = MagicMock()
    mock.percent = percent
    mock.free = int(free_gb * (1024**3))
    return mock


def _virtual_memory(percent: float) -> MagicMock:
    """Return a mock psutil virtual_memory result."""
    mock = MagicMock()
    mock.percent = percent
    return mock


def _run_storage_check(
    disk_percent: float, mem_percent: float, free_gb: float = LARGE_DISK_FREE_GB
):
    """Run _check_storage_health() under controlled psutil mocks."""
    service = HealthCheckService()
    with patch("psutil.disk_usage", return_value=_disk_usage(disk_percent, free_gb)):
        with patch("psutil.virtual_memory", return_value=_virtual_memory(mem_percent)):
            return service, service._check_storage_health()


@pytest.fixture
def healthy_disk_high_memory_storage_result():
    """Storage check result: disk healthy, memory elevated."""
    _svc, result = _run_storage_check(HEALTHY_DISK_PERCENT, HIGH_MEMORY_PERCENT)
    return result


@pytest.fixture
def warning_disk_normal_memory_overall():
    """Overall failure_reasons: disk at warning level, memory fine."""
    service = HealthCheckService()
    system_info = SystemHealthInfo(
        memory_usage_percent=NORMAL_MEMORY_PERCENT,
        cpu_usage_percent=NORMAL_CPU_PERCENT,
        active_jobs=0,
        disk_free_space_gb=LOW_DISK_FREE_GB,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
    )
    with patch(
        "psutil.disk_usage",
        return_value=_disk_usage(WARNING_DISK_PERCENT, LOW_DISK_FREE_GB),
    ):
        with patch(
            "psutil.virtual_memory", return_value=_virtual_memory(NORMAL_MEMORY_PERCENT)
        ):
            storage_svc = service._check_storage_health()
    _status, failure_reasons = service._calculate_overall_status(
        {"storage": storage_svc}, system_info, []
    )
    return failure_reasons


class TestStorageCheckDiskOnly:
    """_check_storage_health() must examine disk only, not RAM."""

    def test_storage_health_no_ram_in_error_message(
        self, healthy_disk_high_memory_storage_result
    ):
        """Storage error_message must be None when disk is healthy, regardless of RAM.

        Previously the storage check set error_message='High memory usage: X%'
        which caused a duplicate 'Storage: High memory usage: X%' failure reason.
        """
        result = healthy_disk_high_memory_storage_result
        assert result.error_message is None, (
            f"_check_storage_health() set error_message={result.error_message!r} "
            "but storage must be disk-only; RAM is reported by _collect_resource_failures()"
        )

    def test_storage_status_healthy_when_disk_ok_regardless_of_memory(self):
        """Storage status is HEALTHY when disk is fine, even if RAM is elevated."""
        _svc, result = _run_storage_check(HEALTHY_DISK_PERCENT, CRITICAL_MEMORY_PERCENT)
        assert result.status == HealthStatus.HEALTHY, (
            f"Storage status={result.status!r} but disk is healthy; "
            "memory pressure must not affect storage status"
        )


class TestStorageFailureReasons:
    """_calculate_overall_status() failure_reasons must not mix RAM into Storage label."""

    def test_storage_health_disk_ok_memory_high_no_storage_failure_reason(self):
        """No Storage: entry at all when only RAM is elevated and disk is healthy.

        Reproduces Bug #737: memory=85%, disk=30%.  failure_reasons must contain
        exactly one RAM: entry and zero Storage: entries (regardless of wording).
        """
        service = HealthCheckService()
        system_info = SystemHealthInfo(
            memory_usage_percent=HIGH_MEMORY_PERCENT,
            cpu_usage_percent=NORMAL_CPU_PERCENT,
            active_jobs=0,
            disk_free_space_gb=LARGE_DISK_FREE_GB,
            disk_read_kb_s=0.0,
            disk_write_kb_s=0.0,
            net_rx_kb_s=0.0,
            net_tx_kb_s=0.0,
        )
        with patch("psutil.disk_usage", return_value=_disk_usage(HEALTHY_DISK_PERCENT)):
            with patch(
                "psutil.virtual_memory",
                return_value=_virtual_memory(HIGH_MEMORY_PERCENT),
            ):
                storage_svc = service._check_storage_health()

        _status, failure_reasons = service._calculate_overall_status(
            {"storage": storage_svc}, system_info, []
        )

        ram_entries = [r for r in failure_reasons if r.startswith("RAM:")]
        assert len(ram_entries) == 1, (
            f"Expected exactly 1 RAM: entry, got {ram_entries}"
        )

        # No Storage: entry at all — disk is healthy so storage must be silent
        storage_entries = [
            r for r in failure_reasons if r.lower().startswith("storage:")
        ]
        assert storage_entries == [], (
            f"Bug #737: unexpected Storage: entries when disk is healthy: {storage_entries}"
        )

    def test_storage_health_disk_warning_is_reported(
        self, warning_disk_normal_memory_overall
    ):
        """Disk warning still produces a Storage: failure reason after the fix.

        Ensures the fix does not remove legitimate disk-related failure reasons.
        """
        failure_reasons = warning_disk_normal_memory_overall
        storage_entries = [
            r for r in failure_reasons if r.lower().startswith("storage:")
        ]
        assert len(storage_entries) >= 1, (
            f"Expected at least one Storage: entry for high disk usage, got: {failure_reasons}"
        )
        assert not any("memory" in e.lower() for e in storage_entries), (
            f"Storage entries must not mention memory: {storage_entries}"
        )
