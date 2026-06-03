"""Bug #1046: CowDaemonBackend._translate_to_daemon_path must resolve symlinks.

On staging, golden_repos_dir is a symlink:
  /home/jsbattig/.cidx-server/data/golden-repos -> /mnt/cow-storage/golden-repos

The literal startswith check rejected the symlink path even though os.path.realpath()
resolves it correctly under mount_point.

Tests use real os.symlink() calls on tmp_path — NOT mocks.
"""

import os

import pytest

from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
from code_indexer.server.utils.config_manager import CowDaemonConfig


def _make_backend(mount_point: str, daemon_storage_path: str) -> CowDaemonBackend:
    """Construct a CowDaemonBackend with the given mount/daemon paths."""
    config = CowDaemonConfig(
        daemon_url="http://localhost:8765",
        api_key="test-key",
        mount_point=mount_point,
        daemon_storage_path=daemon_storage_path,
    )
    return CowDaemonBackend(config=config)


class TestTranslateToDaemonPathSymlinkResolution:
    """AC2: symlink path must be translated without raising ValueError."""

    def test_symlink_path_is_translated(self, tmp_path):
        """Bug #1046 regression: a symlink path under mount_point must translate correctly.

        Layout:
          tmp_path/mount/golden-repos/   <- real directory (is mount_point)
          tmp_path/link/                 <- symlink -> tmp_path/mount/golden-repos/

        _translate_to_daemon_path(str(tmp_path/"link/cidx-meta")) must return
        the daemon-side path without raising ValueError.
        """
        mount_dir = tmp_path / "mount" / "golden-repos"
        mount_dir.mkdir(parents=True)

        link_dir = tmp_path / "link"
        os.symlink(str(mount_dir), str(link_dir))

        mount_point = str(mount_dir)
        daemon_storage_path = "/daemon-storage/golden-repos"

        backend = _make_backend(mount_point, daemon_storage_path)

        symlink_path = str(link_dir / "cidx-meta")
        result = backend._translate_to_daemon_path(symlink_path)

        assert result == daemon_storage_path + "/cidx-meta"

    def test_direct_path_still_works(self, tmp_path):
        """AC3: non-symlink direct path under mount_point continues to translate correctly."""
        mount_dir = tmp_path / "mount" / "golden-repos"
        mount_dir.mkdir(parents=True)

        mount_point = str(mount_dir)
        daemon_storage_path = "/daemon-storage/golden-repos"

        backend = _make_backend(mount_point, daemon_storage_path)

        direct_path = mount_point + "/some-repo"
        result = backend._translate_to_daemon_path(direct_path)

        assert result == daemon_storage_path + "/some-repo"

    def test_outside_mount_raises_value_error(self, tmp_path):
        """AC4: path whose realpath is outside mount_point must still raise ValueError."""
        mount_dir = tmp_path / "mount" / "golden-repos"
        mount_dir.mkdir(parents=True)

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir(parents=True)

        # Create a symlink that points OUTSIDE mount_point
        link_outside = tmp_path / "link-outside"
        os.symlink(str(outside_dir), str(link_outside))

        mount_point = str(mount_dir)
        daemon_storage_path = "/daemon-storage/golden-repos"

        backend = _make_backend(mount_point, daemon_storage_path)

        with pytest.raises(ValueError, match="cannot translate to daemon view"):
            backend._translate_to_daemon_path(str(link_outside / "cidx-meta"))

    def test_symlink_to_daemon_storage_path_passes_through(self, tmp_path):
        """Daemon-host layout: symlink whose realpath resolves under daemon_storage_path.

        Layout:
          tmp_path/daemon-storage/golden-repos/   <- real directory (is daemon_storage_path)
          tmp_path/link/                           <- symlink -> tmp_path/daemon-storage/golden-repos/

        _translate_to_daemon_path(str(tmp_path/"link/cidx-meta")) must return the
        resolved daemon-storage path unchanged — no re-prefixing with mount_point.
        """
        daemon_dir = tmp_path / "daemon-storage" / "golden-repos"
        daemon_dir.mkdir(parents=True)

        link_dir = tmp_path / "link"
        os.symlink(str(daemon_dir), str(link_dir))

        mount_point = str(tmp_path / "mount" / "golden-repos")
        daemon_storage_path = str(daemon_dir)

        backend = _make_backend(mount_point, daemon_storage_path)

        symlink_path = str(link_dir / "cidx-meta")
        result = backend._translate_to_daemon_path(symlink_path)

        assert result == str(daemon_dir / "cidx-meta")

    def test_direct_daemon_storage_path_passes_through(self, tmp_path):
        """Daemon-host layout: literal (non-symlink) path under daemon_storage_path.

        A path already under daemon_storage_path must be returned as-is without
        mount_point translation.
        """
        daemon_dir = tmp_path / "daemon-storage" / "golden-repos"
        daemon_dir.mkdir(parents=True)

        mount_point = str(tmp_path / "mount" / "golden-repos")
        daemon_storage_path = str(daemon_dir)

        backend = _make_backend(mount_point, daemon_storage_path)

        direct_path = str(daemon_dir / "some-repo")
        result = backend._translate_to_daemon_path(direct_path)

        assert result == direct_path
