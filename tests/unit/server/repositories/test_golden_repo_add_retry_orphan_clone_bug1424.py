"""Regression: add_golden_repo retry must clean an orphan partial clone (H1).

Pod-pull work-stealing (PR #1424) made add_golden_repo cross-node
reclaim-eligible. A hard crash (SIGKILL) mid-clone leaves a partial clone dir
at golden_repos_dir/{alias} with NO committed golden_repos row -- the
except-path _cleanup_failed_clone never ran. On dead-node reclaim + retry,
_clone_repository raises "destination path already exists" BEFORE
_clone_path_for_cleanup is assigned, so nothing is ever cleaned and every retry
AND every future manual re-add of that alias fails forever.

Fix: execute_add_golden_repo_work proactively removes such an ORPHAN clone via
_remove_orphan_clone_for_retry(repo_url, alias) before cloning. Guards:
  - never touch in-place-registration content (EVO-64228)
  - never remove a clone whose golden_repos row IS committed (near-complete
    registration, not an orphan)
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
)


@pytest.fixture()
def manager():
    tmp = tempfile.mkdtemp()
    try:
        mgr = GoldenRepoManager(data_dir=tmp)
        os.makedirs(mgr.golden_repos_dir, exist_ok=True)
        yield mgr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_orphan_clone(manager: GoldenRepoManager, alias: str) -> str:
    """Create a non-empty partial-clone dir at golden_repos_dir/{alias}."""
    clone_path = os.path.join(manager.golden_repos_dir, alias)
    os.makedirs(clone_path, exist_ok=True)
    with open(os.path.join(clone_path, "partial.txt"), "w") as fh:
        fh.write("partial clone leftover")
    return clone_path


class TestRemoveOrphanCloneForRetry:
    def test_removes_orphan_clone_when_no_committed_row(self, manager):
        clone_path = _make_orphan_clone(manager, "repoA")
        assert os.path.exists(clone_path)

        removed = manager._remove_orphan_clone_for_retry(
            "https://github.com/org/repoA.git", "repoA"
        )

        assert removed is True
        assert not os.path.exists(clone_path)

    def test_keeps_clone_when_committed_row_exists(self, manager):
        clone_path = _make_orphan_clone(manager, "repoB")
        # A committed golden_repos row => NOT an orphan (near-complete register).
        manager._sqlite_backend.add_repo(
            alias="repoB",
            repo_url="https://github.com/org/repoB.git",
            default_branch="main",
            clone_path=clone_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )

        removed = manager._remove_orphan_clone_for_retry(
            "https://github.com/org/repoB.git", "repoB"
        )

        assert removed is False
        assert os.path.exists(clone_path)

    def test_skips_in_place_registration(self, manager):
        # repo_url == clone_path => in-place; content is not cidx's to remove.
        clone_path = _make_orphan_clone(manager, "repoC")
        removed = manager._remove_orphan_clone_for_retry(clone_path, "repoC")

        assert removed is False
        assert os.path.exists(clone_path)

    def test_noop_when_no_clone_dir(self, manager):
        clone_path = os.path.join(manager.golden_repos_dir, "repoD")
        assert not os.path.exists(clone_path)

        removed = manager._remove_orphan_clone_for_retry(
            "https://github.com/org/repoD.git", "repoD"
        )

        assert removed is False


class TestExecuteAddRetryDoesNotWedgeOnOrphanClone:
    """The real execute_add_golden_repo_work retry path (no mocking of the
    manager's own methods): a pre-existing orphan clone is removed before the
    real clone runs, so the retry fails for the REAL underlying reason (bogus
    source) rather than wedging forever on "destination path already exists"."""

    def test_orphan_marker_removed_before_real_clone(self, manager):
        clone_path = _make_orphan_clone(manager, "repoE")
        marker = os.path.join(clone_path, "partial.txt")
        assert os.path.exists(marker)

        # A local (not in-place) source that does not exist: _clone_repository
        # runs for real and fails fast with no network. The point is that the
        # orphan is cleaned BEFORE that clone attempt.
        bogus_source = os.path.join(manager.data_dir, "nonexistent-source-repoE")
        assert not os.path.exists(bogus_source)

        with pytest.raises(Exception):
            manager.execute_add_golden_repo_work(
                repo_url=bogus_source,
                alias="repoE",
            )

        # The orphan leftover MUST be gone -- proving cleanup ran before clone.
        # (Before the fix, clone raises "destination path already exists" and
        # the marker survives.)
        assert not os.path.exists(marker)
