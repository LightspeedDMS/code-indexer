"""
Unit tests for NFS Golden Repo Volume Monitoring on Dashboard.

Story #1002: As a cluster administrator, I want to see NFS golden repo volume
usage on the main dashboard alongside local disk metrics.

AC1: NFS volume included in cluster mode with accurate metrics and correct fstype.
AC2: NFS volume absent in standalone mode (no nfs_validator).
AC3: NFS volume absent when mount is down.
AC4: NFS volume skipped gracefully when psutil.disk_usage() raises OSError.
"""

import pytest
from unittest.mock import MagicMock, patch

from code_indexer.server.services.health_service import HealthCheckService


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

NFS_MOUNT_PATH = "/mnt/cidx-shared"
NFS_FSTYPE = "nfs4"
NFS_DEVICE_LABEL = "Golden Repos (NFS)"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_nfs_validator(
    mount_path: str = NFS_MOUNT_PATH, is_mounted: bool = True
) -> MagicMock:
    """Return a minimal nfs_validator mock with the two required methods."""
    validator = MagicMock()
    validator.get_mount_path.return_value = mount_path
    validator.is_mounted.return_value = is_mounted
    return validator


def _make_disk_usage(
    total_gb: float = 200.0,
    used_gb: float = 80.0,
    free_gb: float = 120.0,
    used_percent: float = 40.0,
) -> MagicMock:
    mock = MagicMock()
    mock.total = int(total_gb * 1024**3)
    mock.used = int(used_gb * 1024**3)
    mock.free = int(free_gb * 1024**3)
    mock.percent = used_percent
    return mock


def _make_nfs_partition(
    mountpoint: str = NFS_MOUNT_PATH, fstype: str = NFS_FSTYPE
) -> MagicMock:
    """Return a psutil partition mock representing the NFS mount."""
    p = MagicMock()
    p.mountpoint = mountpoint
    p.fstype = fstype
    p.device = "nfs-server:/export/cidx"
    return p


def _get_nfs_entries(volumes: list) -> list:
    """Return only the NFS VolumeInfo entries from a volume list."""
    return [v for v in volumes if v.device == NFS_DEVICE_LABEL]


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestNfsVolumeIncluded:
    """AC1: NFS volume entry appears in cluster mode with correct metrics and fstype."""

    def test_nfs_volume_metrics_and_fstype_in_cluster_mode(self):
        """
        When nfs_validator is provided AND is_mounted() is True,
        _get_mounted_volumes() must append a VolumeInfo with:
          - device == "Golden Repos (NFS)"
          - mount_point == nfs_validator.get_mount_path()
          - fstype from the matching psutil partition (e.g. 'nfs4')
          - metrics computed from psutil.disk_usage(mount_path)
        """
        service = HealthCheckService(
            nfs_validator=_make_nfs_validator(
                mount_path=NFS_MOUNT_PATH, is_mounted=True
            )
        )
        disk_usage = _make_disk_usage(
            total_gb=200.0, used_gb=80.0, free_gb=120.0, used_percent=40.0
        )

        with (
            patch(
                "psutil.disk_partitions",
                return_value=[_make_nfs_partition(NFS_MOUNT_PATH, "nfs4")],
            ),
            patch("psutil.disk_usage", return_value=disk_usage),
        ):
            volumes = service._get_mounted_volumes()

        entries = _get_nfs_entries(volumes)
        assert len(entries) == 1, f"Expected one NFS entry, got: {volumes}"
        e = entries[0]
        assert e.mount_point == NFS_MOUNT_PATH
        assert e.fstype == "nfs4"
        assert e.total_gb == pytest.approx(200.0, rel=0.01)
        assert e.used_gb == pytest.approx(80.0, rel=0.01)
        assert e.free_gb == pytest.approx(120.0, rel=0.01)
        assert e.used_percent == pytest.approx(40.0, rel=0.01)
        assert e.free_percent == pytest.approx(60.0, rel=0.01)


class TestNfsVolumeAbsent:
    """AC2 + AC3: NFS volume absent in standalone mode or when unmounted."""

    @pytest.mark.parametrize(
        "description,nfs_validator",
        [
            ("standalone_no_validator", None),
            (
                "cluster_unmounted",
                _make_nfs_validator(mount_path=NFS_MOUNT_PATH, is_mounted=False),
            ),
        ],
    )
    def test_nfs_volume_absent(
        self, description: str, nfs_validator: MagicMock
    ) -> None:
        """
        NFS entry must NOT appear when:
          - standalone mode (nfs_validator is None), or
          - cluster mode but nfs_validator.is_mounted() is False.
        """
        service = HealthCheckService(nfs_validator=nfs_validator)

        with patch("psutil.disk_partitions", return_value=[]):
            volumes = service._get_mounted_volumes()

        assert _get_nfs_entries(volumes) == [], (
            f"[{description}] NFS entry must be absent, but got: {volumes}"
        )


class TestNfsVolumeOsErrorSkipped:
    """AC4: OSError from psutil.disk_usage() is handled gracefully."""

    def test_nfs_volume_skipped_on_oserror_local_volumes_unaffected(self):
        """
        When psutil.disk_usage(nfs_mount_path) raises OSError,
        _get_mounted_volumes() must skip the NFS entry without raising,
        and local disk volumes must still be present.
        """
        service = HealthCheckService(
            nfs_validator=_make_nfs_validator(
                mount_path=NFS_MOUNT_PATH, is_mounted=True
            )
        )

        local_partition = MagicMock()
        local_partition.mountpoint = "/data"
        local_partition.fstype = "ext4"
        local_partition.device = "/dev/sda1"

        local_disk_usage = _make_disk_usage(
            total_gb=500.0, used_gb=200.0, free_gb=300.0, used_percent=40.0
        )

        def disk_usage_side_effect(path: str):
            if path == NFS_MOUNT_PATH:
                raise OSError("NFS mount stale")
            return local_disk_usage

        with (
            patch("psutil.disk_partitions", return_value=[local_partition]),
            patch("psutil.disk_usage", side_effect=disk_usage_side_effect),
        ):
            volumes = service._get_mounted_volumes()

        assert _get_nfs_entries(volumes) == [], (
            f"NFS entry must be absent on OSError, got: {volumes}"
        )
        local_entries = [v for v in volumes if v.mount_point == "/data"]
        assert len(local_entries) == 1, (
            f"Local volumes must survive NFS OSError, got: {volumes}"
        )


class TestNodeMetricsWriterNfsVolume:
    """Verify _collect_volume_info includes NFS volume when validator provided."""

    def test_collect_volume_info_includes_nfs_when_validator_mounted(self):
        """NFS dict entry appears in volumes when mounted validator is provided."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_volume_info,
        )

        validator = _make_nfs_validator(mount_path=NFS_MOUNT_PATH, is_mounted=True)
        disk_usage = _make_disk_usage(
            total_gb=200.0, used_gb=80.0, free_gb=120.0, used_percent=40.0
        )

        with (
            patch(
                "psutil.disk_partitions",
                return_value=[_make_nfs_partition(NFS_MOUNT_PATH, "nfs4")],
            ),
            patch("psutil.disk_usage", return_value=disk_usage),
        ):
            volumes = _collect_volume_info(nfs_validator=validator)

        nfs_entries = [v for v in volumes if v.get("device") == NFS_DEVICE_LABEL]
        assert len(nfs_entries) == 1
        assert nfs_entries[0]["mount_point"] == NFS_MOUNT_PATH
        assert nfs_entries[0]["fstype"] == "nfs4"

    def test_collect_volume_info_excludes_nfs_when_no_validator(self):
        """NFS entry absent when nfs_validator=None even if NFS partition is present."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_volume_info,
        )

        disk_usage = _make_disk_usage(
            total_gb=200.0, used_gb=80.0, free_gb=120.0, used_percent=40.0
        )

        # NFS partition IS present in the system — exclusion must come from missing validator
        with (
            patch(
                "psutil.disk_partitions",
                return_value=[_make_nfs_partition(NFS_MOUNT_PATH, "nfs4")],
            ),
            patch("psutil.disk_usage", return_value=disk_usage),
        ):
            volumes = _collect_volume_info(nfs_validator=None)

        assert [v for v in volumes if v.get("device") == NFS_DEVICE_LABEL] == []
