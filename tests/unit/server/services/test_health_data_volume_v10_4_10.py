"""
Regression tests for Bug #71: health badge + storage alerts use root volume
instead of data_directory.

v10.4.10 fix: _check_storage_health() and SystemMetricsCollector both resolve
server_dir from config and call psutil.disk_usage() on the data volume, not '/'.
Per-volume progress bars (dashboard) are unaffected — they still use
partition.mountpoint.
"""

from unittest.mock import MagicMock, patch, call

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.services.system_metrics_collector import SystemMetricsCollector


# Synthetic test path — clearly not environment-coupled
FAKE_DATA_DIR = "/fake-cidx-data"

# Patch targets
# health_service uses a lazy `from code_indexer.server.services.config_service import
# get_config_service` inside _check_storage_health(). Patching the source module
# ensures both the module-level and lazy imports see the mock.
_CONFIG_SVC_SOURCE = "code_indexer.server.services.config_service.get_config_service"
_HEALTH_OS_ISDIR = "code_indexer.server.services.health_service.os.path.isdir"
_METRICS_OS_ISDIR = (
    "code_indexer.server.services.system_metrics_collector.os.path.isdir"
)


def _make_config(server_dir: str = FAKE_DATA_DIR) -> MagicMock:
    cfg = MagicMock()
    cfg.server_dir = server_dir
    return cfg


def _make_disk_usage(percent: float = 30.0, free_gb: float = 100.0) -> MagicMock:
    mock = MagicMock()
    mock.percent = percent
    mock.free = int(free_gb * 1024**3)
    return mock


class TestStorageHealthDataVolume:
    """_check_storage_health() and _resolve_data_path() must use server_dir, not '/'."""

    def test_check_storage_health_uses_data_directory_volume(self):
        """
        When config returns server_dir and that path exists as a directory,
        _check_storage_health() must call psutil.disk_usage(server_dir), NOT '/'.
        """
        service = HealthCheckService()
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_config(FAKE_DATA_DIR)
        disk_mock = _make_disk_usage(30.0)

        with (
            patch(_CONFIG_SVC_SOURCE, return_value=mock_config_svc),
            patch(_HEALTH_OS_ISDIR, return_value=True),
            patch("psutil.disk_usage", return_value=disk_mock) as mock_disk,
        ):
            service._check_storage_health()

        mock_disk.assert_called_once_with(FAKE_DATA_DIR)
        assert mock_disk.call_args != call("/"), (
            "_check_storage_health() must use the data directory volume, not '/'"
        )

    def test_check_storage_health_falls_back_to_root_when_config_unavailable(self):
        """
        When get_config_service() raises RuntimeError (config not yet initialized),
        _check_storage_health() must fall back to '/' and not crash.
        """
        service = HealthCheckService()
        disk_mock = _make_disk_usage(30.0)

        with (
            patch(_CONFIG_SVC_SOURCE, side_effect=RuntimeError("config not ready")),
            patch("psutil.disk_usage", return_value=disk_mock) as mock_disk,
        ):
            result = service._check_storage_health()

        assert result is not None
        mock_disk.assert_called_once_with("/")

    def test_get_system_info_uses_data_directory_volume(self):
        """
        _resolve_data_path() returns server_dir when config is available
        and the path exists as a directory.
        """
        collector = SystemMetricsCollector.__new__(SystemMetricsCollector)
        mock_config_svc = MagicMock()
        mock_config_svc.get_config.return_value = _make_config(FAKE_DATA_DIR)

        with (
            patch(_CONFIG_SVC_SOURCE, return_value=mock_config_svc),
            patch(_METRICS_OS_ISDIR, return_value=True),
        ):
            result = collector._resolve_data_path()

        assert result == FAKE_DATA_DIR, (
            f"_resolve_data_path() returned {result!r}, expected {FAKE_DATA_DIR!r}"
        )

    def test_per_volume_progressbars_still_use_partition_mountpoint_unchanged(self):
        """
        _get_mounted_volumes() must still call psutil.disk_usage(partition.mountpoint)
        for each partition — the #71 fix must not change this per-volume enumeration.
        """
        service = HealthCheckService()

        fake_partition = MagicMock()
        fake_partition.mountpoint = "/fake-mount-point"
        fake_partition.fstype = "ext4"
        fake_partition.device = "/dev/sda1"

        disk_mock = _make_disk_usage(40.0, 500.0)

        with (
            patch("psutil.disk_partitions", return_value=[fake_partition]),
            patch("psutil.disk_usage", return_value=disk_mock) as mock_disk,
        ):
            service._get_mounted_volumes()

        mock_disk.assert_called_with("/fake-mount-point")
