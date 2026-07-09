"""
Tests for Bug #1246: symlink-aware path confinement in ActivatedRepoIndexManager.

On cow-daemon backends, data_dir/activated-repos is a symlink to the cow-daemon
mount (e.g. /mnt/cow-storage/activated-repos). The original confinement check
called Path(repo_path).resolve() which follows the symlink, then checked
relative_to(data_dir.resolve()). Since data_dir is NOT itself a symlink,
data_dir.resolve() does NOT include the cow mount path, so the check always
raised ValueError("Security violation...") for every cow-daemon repo.

Fix: compute the set of allowed resolved roots (data_dir itself PLUS the
resolved symlink targets of data_dir/activated-repos and data_dir/golden-repos)
and accept the repo if its resolved path is under ANY of them.

RED tests: current code fails test_symlinked_activated_repos_accepted and
test_golden_repos_symlink_accepted.
"""

import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bgm_mock() -> Mock:
    """BackgroundJobManager mock that satisfies trigger_reindex's pre-flight checks."""
    bgm = Mock(spec=BackgroundJobManager)
    bgm.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    bgm.submit_job = Mock(return_value=str(uuid.uuid4()))
    return bgm


def _make_manager(
    data_dir: Path, repo_path: str, bgm: Mock
) -> ActivatedRepoIndexManager:
    """Build an ActivatedRepoIndexManager with a mocked activated_repo_manager."""
    arm = Mock()
    arm.get_activated_repo_path = Mock(return_value=repo_path)
    return ActivatedRepoIndexManager(
        data_dir=str(data_dir),
        background_job_manager=bgm,
        activated_repo_manager=arm,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPathConfinementSymlinkAware:
    """Bug #1246: confinement check must accept paths reached via symlinks."""

    def test_symlinked_activated_repos_accepted(self, tmp_path: Path) -> None:
        """
        Cow-daemon case: data_dir/activated-repos is a symlink to an external mount.
        A repo path that resolves OUTSIDE data_dir (via the symlink) must be ACCEPTED.

        This test FAILS on the original code because Path(repo_path).resolve()
        follows the symlink to cow_root, which is not under data_dir.resolve().
        """
        # Simulate cow-daemon storage (e.g. /mnt/cow-storage/activated-repos)
        cow_root = tmp_path / "cow" / "activated-repos"
        repo_real = cow_root / "admin" / "my-repo"
        repo_real.mkdir(parents=True)

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create the symlink: data_dir/activated-repos -> cow/activated-repos
        (data_dir / "activated-repos").symlink_to(cow_root)

        # activated_repo_manager returns the LOGICAL path (through symlink)
        logical_repo_path = str(data_dir / "activated-repos" / "admin" / "my-repo")

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, logical_repo_path, bgm)

        # Must NOT raise; must reach and call submit_job
        job_id = manager.trigger_reindex("my-repo", ["semantic"], False, "admin")

        assert job_id is not None
        bgm.submit_job.assert_called_once()

    def test_golden_repos_symlink_accepted(self, tmp_path: Path) -> None:
        """
        Cow-daemon: data_dir/golden-repos is also a symlink to the cow mount.
        Paths resolving through golden-repos symlink must also be accepted.

        This test FAILS on the original code for the same reason.
        """
        cow_golden = tmp_path / "cow" / "golden-repos"
        repo_real = cow_golden / "my-golden-repo"
        repo_real.mkdir(parents=True)

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        (data_dir / "golden-repos").symlink_to(cow_golden)

        logical_repo_path = str(data_dir / "golden-repos" / "my-golden-repo")

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, logical_repo_path, bgm)

        job_id = manager.trigger_reindex("my-golden-repo", ["semantic"], False, "admin")

        assert job_id is not None
        bgm.submit_job.assert_called_once()

    def test_local_backend_no_symlink_accepted(self, tmp_path: Path) -> None:
        """
        Local backend: repo is a real directory under data_dir/activated-repos.
        Pre-fix behavior must be preserved (repo resolves inside data_dir).
        """
        data_dir = tmp_path / "data"
        repo_dir = data_dir / "activated-repos" / "admin" / "local-repo"
        repo_dir.mkdir(parents=True)

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, str(repo_dir), bgm)

        job_id = manager.trigger_reindex("local-repo", ["semantic"], False, "admin")

        assert job_id is not None
        bgm.submit_job.assert_called_once()

    def test_genuine_traversal_rejected(self, tmp_path: Path) -> None:
        """
        A path resolving outside ALL allowed roots must still raise ValueError.
        This verifies the security check is not weakened by the fix.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Create activated-repos so it exists but repo is outside everything
        (data_dir / "activated-repos").mkdir()

        escape_dir = tmp_path / "escape-root" / "evil"
        escape_dir.mkdir(parents=True)

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, str(escape_dir), bgm)

        with pytest.raises(
            ValueError,
            match="Security violation: Repository path escapes data directory",
        ):
            manager.trigger_reindex("evil-repo", ["semantic"], False, "admin")

    def test_sibling_prefix_bypass_rejected(self, tmp_path: Path) -> None:
        """
        Bug #1246 adversarial case: a sibling directory whose name shares a
        string PREFIX with the allowed symlinked root (e.g.
        "activated-repos-evil" vs "activated-repos") must NOT be accepted.

        This is the exact attack Path.relative_to() defends against: a naive
        str(repo_path).startswith(str(root)) check would incorrectly accept
        ".../cow/activated-repos-evil/admin/escapehere" because the STRING
        ".../cow/activated-repos-evil" starts with the STRING
        ".../cow/activated-repos". relative_to() compares path components,
        not characters, so "activated-repos-evil" is correctly recognized as
        a SIBLING of "activated-repos", not a sub-path of it, and the evil
        path is rejected. The paired positive assertion below proves the
        real allowed root (reached through the same symlink) is still
        accepted, demonstrating allowed-vs-sibling discrimination.
        """
        cow_dir = tmp_path / "cow"
        cow_dir.mkdir()

        # The allowed root: symlink target for data_dir/activated-repos.
        allowed_root = cow_dir / "activated-repos"
        good_repo = allowed_root / "admin" / "good-repo"
        good_repo.mkdir(parents=True)

        # The sibling "evil" dir: shares the string prefix "activated-repos"
        # but is NOT a sub-path of allowed_root.
        evil_repo = cow_dir / "activated-repos-evil" / "admin" / "escapehere"
        evil_repo.mkdir(parents=True)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "activated-repos").symlink_to(allowed_root)

        # Negative: the sibling-prefix path must be REJECTED.
        evil_bgm = _make_bgm_mock()
        evil_manager = _make_manager(data_dir, str(evil_repo), evil_bgm)
        with pytest.raises(
            ValueError,
            match="Security violation: Repository path escapes data directory",
        ):
            evil_manager.trigger_reindex("escapehere", ["semantic"], False, "admin")
        evil_bgm.submit_job.assert_not_called()

        # Positive: the genuine allowed path (reached via the same symlink)
        # must still be ACCEPTED, proving discrimination, not over-rejection.
        good_bgm = _make_bgm_mock()
        logical_good_path = str(data_dir / "activated-repos" / "admin" / "good-repo")
        good_manager = _make_manager(data_dir, logical_good_path, good_bgm)
        job_id = good_manager.trigger_reindex("good-repo", ["semantic"], False, "admin")
        assert job_id is not None
        good_bgm.submit_job.assert_called_once()

    def test_absolute_path_outside_all_roots_rejected(self, tmp_path: Path) -> None:
        """
        A plain absolute path that resolves outside every allowed root
        (data_dir itself and any symlinked activated-repos/golden-repos
        targets) must be rejected. No symlink or dot-dot token is involved
        here — this is the plain "nowhere near an allowed root" case.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "activated-repos").mkdir()

        # A real directory entirely outside data_dir; no symlink involved.
        escape_dir = tmp_path / "outside"
        escape_dir.mkdir()

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, str(escape_dir), bgm)

        with pytest.raises(
            ValueError,
            match="Security violation: Repository path escapes data directory",
        ):
            manager.trigger_reindex("outside-repo", ["semantic"], False, "admin")

    def test_multiple_index_types_symlink_accepted(self, tmp_path: Path) -> None:
        """
        Cow-daemon symlink case with multiple index types (semantic + fts).
        The confinement check fires before index-type dispatch, so the fix
        must work regardless of which types are requested.
        """
        cow_root = tmp_path / "cow" / "activated-repos"
        repo_real = cow_root / "admin" / "multi-repo"
        repo_real.mkdir(parents=True)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "activated-repos").symlink_to(cow_root)

        logical_repo_path = str(data_dir / "activated-repos" / "admin" / "multi-repo")

        bgm = _make_bgm_mock()
        manager = _make_manager(data_dir, logical_repo_path, bgm)

        job_id = manager.trigger_reindex(
            "multi-repo", ["semantic", "fts"], False, "admin"
        )

        assert job_id is not None
        bgm.submit_job.assert_called_once()
