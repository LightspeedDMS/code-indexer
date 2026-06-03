"""Bug #1044/#1046 regression: ActivatedRepoManager resolves symlinks on activated_repos_dir.

Cluster deployments make the activated-repos directory a symlink into the CoW
storage so the daemon can clone into its managed filesystem. The deactivation
helpers open this directory with O_NOFOLLOW (security against tampering inside
the tree), which fails with ENOTDIR if the top-level path itself is a symlink.

Resolving the admin-controlled top-level path at construction time keeps O_NOFOLLOW
protection on user/alias subpaths intact while fixing the symlinked-deployment case.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


class TestActivatedReposDirSymlinkResolution:
    def test_symlinked_data_dir_resolves(self, tmp_path):
        # Layout:
        #   tmp_path/
        #     real_storage/   <- actual storage
        #     data/
        #       activated-repos -> ../real_storage/activated-repos (symlink)
        real_storage = tmp_path / "real_storage"
        real_storage.mkdir()
        target = real_storage / "activated-repos"
        target.mkdir()

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Symlink the activated-repos path into real_storage
        os.symlink(str(target), str(data_dir / "activated-repos"))

        arm = ActivatedRepoManager(
            data_dir=str(data_dir),
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )

        # Must be resolved: equal to realpath of target, not the symlink path
        assert arm.activated_repos_dir == os.path.realpath(str(target))
        assert os.path.islink(arm.activated_repos_dir) is False

    def test_non_symlink_directory_still_works(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        arm = ActivatedRepoManager(
            data_dir=str(data_dir),
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )

        expected = os.path.realpath(str(data_dir / "activated-repos"))
        assert arm.activated_repos_dir == expected
        assert os.path.isdir(arm.activated_repos_dir)

    def test_set_shared_repos_dir_resolves_symlink(self, tmp_path):
        real_storage = tmp_path / "shared_storage"
        real_storage.mkdir()
        target = real_storage / "activated-repos"
        target.mkdir()

        link_root = tmp_path / "link_root"
        link_root.mkdir()
        os.symlink(str(target), str(link_root / "activated-repos"))

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        arm = ActivatedRepoManager(
            data_dir=str(data_dir),
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )
        arm.set_shared_repos_dir(str(link_root))

        assert arm.activated_repos_dir == os.path.realpath(str(target))
        assert os.path.islink(arm.activated_repos_dir) is False
