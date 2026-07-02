"""Unit tests for NfsMountValidator.

Strategy: use tmp_path as a stand-in for the mount point.  We patch
os.path.ismount so we can control whether the directory looks like a
real mount without needing root or an actual NFS server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


from code_indexer.server.storage.shared.nfs_validator import NfsMountValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(path: Path) -> NfsMountValidator:
    return NfsMountValidator(str(path))


# ---------------------------------------------------------------------------
# validate() — healthy path
# ---------------------------------------------------------------------------


class TestValidateHealthy:
    """validate() returns a healthy result when mount is accessible and writable."""

    def test_returns_healthy_true(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            result = validator.validate()
        assert result["healthy"] is True

    def test_returns_correct_mount_point(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            result = validator.validate()
        assert result["mount_point"] == str(tmp_path)

    def test_returns_writable_true(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            result = validator.validate()
        assert result["writable"] is True

    def test_returns_non_negative_latency(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            result = validator.validate()
        assert result["latency_ms"] >= 0.0

    def test_returns_no_error(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            result = validator.validate()
        assert result["error"] is None

    def test_probe_file_is_cleaned_up(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            validator.validate()
        probe_files = list(tmp_path.glob(".cidx_nfs_probe_*"))
        assert probe_files == [], f"Probe files not cleaned up: {probe_files}"


# ---------------------------------------------------------------------------
# validate() — unhealthy paths
# ---------------------------------------------------------------------------


class TestValidateUnhealthy:
    """validate() returns unhealthy when the mount is unavailable."""

    def test_missing_mount_point(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist"
        validator = _make_validator(nonexistent)
        result = validator.validate()
        assert result["healthy"] is False
        assert result["error"] is not None
        assert "does not exist" in result["error"]

    def test_not_a_mount_point(self, tmp_path: Path) -> None:
        """Path exists but os.path.ismount returns False."""
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=False):
            result = validator.validate()
        assert result["healthy"] is False
        assert result["writable"] is False
        assert "not a mount point" in result["error"]

    def test_missing_mount_point_writable_false(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no_dir"
        validator = _make_validator(nonexistent)
        result = validator.validate()
        assert result["writable"] is False

    def test_write_failure_returns_unhealthy(self, tmp_path: Path) -> None:
        """Simulate OSError on write by making tmp_path read-only."""
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
                result = validator.validate()
        assert result["healthy"] is False
        assert result["writable"] is False
        assert "NFS write/read probe failed" in result["error"]


# ---------------------------------------------------------------------------
# is_mounted()
# ---------------------------------------------------------------------------


class TestIsMounted:
    """is_mounted() delegates to os.path.ismount."""

    def test_returns_true_when_ismount_true(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=True):
            assert validator.is_mounted() is True

    def test_returns_false_when_not_a_mountpoint(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch("os.path.ismount", return_value=False):
            assert validator.is_mounted() is False

    def test_returns_false_when_path_missing(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "ghost"
        validator = _make_validator(nonexistent)
        # No patch needed — path doesn't exist so exists() short-circuits
        assert validator.is_mounted() is False


# ---------------------------------------------------------------------------
# check_path_accessible()
# ---------------------------------------------------------------------------


class TestCheckPathAccessible:
    """check_path_accessible() checks existence of paths under the mount."""

    def test_existing_absolute_path_returns_true(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir"
        target.mkdir()
        validator = _make_validator(tmp_path)
        assert validator.check_path_accessible(str(target)) is True

    def test_missing_absolute_path_returns_false(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent_subdir"
        validator = _make_validator(tmp_path)
        assert validator.check_path_accessible(str(target)) is False

    def test_relative_path_resolved_under_mount(self, tmp_path: Path) -> None:
        subdir = tmp_path / "clone_xyz"
        subdir.mkdir()
        validator = _make_validator(tmp_path)
        # Pass relative name — should resolve to tmp_path/clone_xyz
        assert validator.check_path_accessible("clone_xyz") is True

    def test_relative_path_missing_returns_false(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        assert validator.check_path_accessible("no_such_clone") is False

    def test_oserror_returns_false(self, tmp_path: Path) -> None:
        validator = _make_validator(tmp_path)
        with patch.object(Path, "exists", side_effect=OSError("NFS stale handle")):
            assert validator.check_path_accessible(str(tmp_path / "whatever")) is False

    def test_accepts_timeout_parameter(self, tmp_path: Path) -> None:
        """Verify the timeout kwarg is accepted without error."""
        target = tmp_path / "dir"
        target.mkdir()
        validator = _make_validator(tmp_path)
        # Should not raise regardless of timeout value
        result = validator.check_path_accessible(str(target), timeout=2.0)
        assert result is True
