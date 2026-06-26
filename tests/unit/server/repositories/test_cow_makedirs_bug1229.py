"""Bug #1229 regression: dangling CoW/NFS symlink must not crash makedirs.

os.makedirs(path, exist_ok=True) raises FileExistsError when `path` is a
dangling symlink (link exists, target gone).  The helper _safe_makedirs_cow
detects this case and logs a degraded-mode WARNING instead of raising, so
worker startup survives a CoW/NFS mount going away.

Tests cover:
1. Dangling symlink → no raise, symlink NOT clobbered, WARNING logged.
2. Missing directory → creates it (normal makedirs behaviour).
3. Existing directory → no-op, no error.
4. Valid symlink → existing real dir: no-op, no error, symlink intact.
5. Manager-level: GoldenRepoManager.__init__ with dangling golden-repos symlink
   does NOT raise.
6. Manager-level: ActivatedRepoManager.__init__ with dangling activated-repos
   symlink does NOT raise.
7. Regression: GoldenRepoManager with a real directory constructs normally.
8. Regression: ActivatedRepoManager with a real directory constructs normally.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from code_indexer.server.utils.cow_utils import _safe_makedirs_cow
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestSafeMakedirsCow:
    """Unit tests for the _safe_makedirs_cow helper function."""

    def test_dangling_symlink_does_not_raise(self, tmp_path, caplog):
        """Dangling symlink: helper returns without raising FileExistsError."""
        missing_target = tmp_path / "missing_target"
        link = tmp_path / "dangling_link"
        os.symlink(str(missing_target), str(link))

        # Precondition: link exists, target does not
        assert os.path.islink(str(link))
        assert not os.path.exists(str(link))

        import logging

        with caplog.at_level(logging.WARNING):
            _safe_makedirs_cow(str(link))  # Must NOT raise

        # Symlink must NOT have been deleted or replaced
        assert os.path.islink(str(link)), "symlink was clobbered"
        assert os.readlink(str(link)) == str(missing_target), (
            "symlink target was changed"
        )

    def test_dangling_symlink_logs_degraded_mode_warning(self, tmp_path, caplog):
        """Dangling symlink: a WARNING containing key terms is logged."""
        missing_target = tmp_path / "nfs_target"
        link = tmp_path / "link_to_missing"
        os.symlink(str(missing_target), str(link))

        import logging

        with caplog.at_level(logging.WARNING):
            _safe_makedirs_cow(str(link))

        warning_text = " ".join(caplog.messages).lower()
        assert (
            "degraded" in warning_text
            or "dangling" in warning_text
            or "unavailable" in warning_text
        ), f"Expected degraded-mode warning, got: {caplog.messages}"

    def test_missing_directory_is_created(self, tmp_path):
        """Normal missing dir: helper creates it just like makedirs."""
        new_dir = tmp_path / "subdir" / "nested"
        assert not new_dir.exists()

        _safe_makedirs_cow(str(new_dir))

        assert new_dir.is_dir()

    def test_existing_directory_is_no_op(self, tmp_path):
        """Existing real dir: helper is a no-op (no error, no change)."""
        existing = tmp_path / "already_exists"
        existing.mkdir()

        _safe_makedirs_cow(str(existing))  # Must NOT raise

        assert existing.is_dir()

    def test_valid_symlink_to_existing_dir_is_no_op(self, tmp_path):
        """Valid symlink pointing at a live dir: no-op, symlink stays intact."""
        real_dir = tmp_path / "real_storage"
        real_dir.mkdir()
        link = tmp_path / "valid_link"
        os.symlink(str(real_dir), str(link))

        _safe_makedirs_cow(str(link))  # Must NOT raise

        assert os.path.islink(str(link)), "symlink was clobbered"
        assert os.readlink(str(link)) == str(real_dir), "symlink target was changed"
        assert os.path.isdir(str(link)), "link no longer resolves to a directory"


# ---------------------------------------------------------------------------
# Manager-level construction tests
# ---------------------------------------------------------------------------


class TestGoldenRepoManagerDanglingSymlink:
    """GoldenRepoManager.__init__ must survive a dangling golden-repos symlink."""

    def test_dangling_golden_repos_symlink_does_not_raise(self, tmp_path, caplog):
        """GoldenRepoManager init with dangling golden-repos symlink does NOT crash."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create golden-repos as a dangling symlink
        missing_cow_mount = tmp_path / "mnt" / "cow-storage" / "golden-repos"
        golden_repos_link = data_dir / "golden-repos"
        os.symlink(str(missing_cow_mount), str(golden_repos_link))

        assert os.path.islink(str(golden_repos_link))
        assert not os.path.exists(str(golden_repos_link))

        import logging

        with caplog.at_level(logging.WARNING):
            # Must NOT raise
            manager = GoldenRepoManager(data_dir=str(data_dir))

        assert manager is not None
        # Symlink must NOT be clobbered (Bug #1052)
        assert os.path.islink(str(golden_repos_link)), "symlink was clobbered"

    def test_normal_data_dir_constructs_normally(self, tmp_path):
        """Regression: GoldenRepoManager with a real directory constructs normally."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        manager = GoldenRepoManager(data_dir=str(data_dir))

        assert manager is not None
        assert os.path.isdir(manager.golden_repos_dir)


class TestActivatedRepoManagerDanglingSymlink:
    """ActivatedRepoManager.__init__ must survive a dangling activated-repos symlink."""

    def test_dangling_activated_repos_symlink_does_not_raise(self, tmp_path, caplog):
        """ActivatedRepoManager init with dangling activated-repos symlink does NOT crash."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create activated-repos as a dangling symlink (CoW mount gone)
        missing_cow_mount = tmp_path / "mnt" / "cow-storage" / "activated-repos"
        activated_repos_link = data_dir / "activated-repos"
        os.symlink(str(missing_cow_mount), str(activated_repos_link))

        assert os.path.islink(str(activated_repos_link))
        assert not os.path.exists(str(activated_repos_link))

        import logging

        with caplog.at_level(logging.WARNING):
            # Must NOT raise
            arm = ActivatedRepoManager(
                data_dir=str(data_dir),
                golden_repo_manager=MagicMock(),
                background_job_manager=MagicMock(),
            )

        assert arm is not None
        # Symlink must NOT be clobbered (Bug #1052)
        assert os.path.islink(str(activated_repos_link)), "symlink was clobbered"

    def test_normal_data_dir_constructs_normally(self, tmp_path):
        """Regression: ActivatedRepoManager with a real directory constructs normally."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        arm = ActivatedRepoManager(
            data_dir=str(data_dir),
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )

        assert arm is not None
        assert os.path.isdir(arm.activated_repos_dir)
