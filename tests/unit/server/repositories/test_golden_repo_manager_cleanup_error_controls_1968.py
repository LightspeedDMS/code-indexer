"""Negative-control regression tests kept from the Bug #1968 investigation.

Issue #1968's framing -- "reclassify FileNotFoundError as success during
clone removal" -- was investigated and paused (see the coordinator's
2026-09-24 course correction). The lock-discipline asymmetry between
`_remove_orphan_clone_for_retry` (unlocked `shutil.rmtree`) and
`remove_golden_repo` (write-lock protected) is the suspected real defect;
reclassifying ENOENT as success would let `add_golden_repo` believe a
concurrently-mid-operation clone is safely gone, risking exactly the
half-destroyed `.git` corruption the `ignore_errors=True` rejection at
`_remove_orphan_clone_for_retry`'s call site guards against. That fix is
deliberately deferred pending a locking design decision.

These tests are kept because they document CURRENT, CORRECT behavior
independent of that decision: a genuine `PermissionError` during clone
removal must still report failure, still log, and still quarantine
(site 1: `_cleanup_filesystem`, reached via `remove_golden_repo`'s
background job) or still raise with nothing silently removed (site 2:
`_remove_orphan_clone_for_retry`, reached via the `add_golden_repo` retry
path, which has no quarantine concept of its own). Whatever the eventual
fix looks like, it must not regress these.
"""

import errno
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.storage.database_manager import DatabaseSchema

MODULE = "code_indexer.server.repositories.golden_repo_manager"
MARKER_CONTENT = "real content"


@pytest.fixture()
def manager():
    tmp = tempfile.mkdtemp()
    try:
        mgr = GoldenRepoManager(data_dir=tmp)
        DatabaseSchema(mgr.db_path).initialize_database()
        os.makedirs(mgr.golden_repos_dir, exist_ok=True)
        yield mgr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_populated_dir(base: str, alias: str) -> str:
    clone_path = os.path.join(base, alias)
    os.makedirs(clone_path, exist_ok=True)
    with open(os.path.join(clone_path, "marker.txt"), "w") as fh:
        fh.write(MARKER_CONTENT)
    return clone_path


def _register_removable_repo(manager: GoldenRepoManager, alias: str) -> Path:
    """Create a real clone dir and register it as a golden repo (both the
    in-memory cache and the SQLite backend), then wire a mocked
    background_job_manager so `remove_golden_repo`'s worker closure can be
    captured and invoked synchronously in the test thread."""
    clone_path = Path(_make_populated_dir(manager.golden_repos_dir, alias))
    repo = GoldenRepo(
        alias=alias,
        repo_url=f"https://example.test/{alias}.git",
        default_branch="main",
        clone_path=str(clone_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    manager.golden_repos[alias] = repo
    manager._sqlite_backend.add_repo(
        alias=repo.alias,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        clone_path=repo.clone_path,
        created_at=repo.created_at,
        enable_temporal=False,
        temporal_options=None,
    )
    manager.background_job_manager = MagicMock()
    manager.background_job_manager.submit_job.return_value = f"remove-job-{alias}"
    # activated_repo_manager is declared non-Optional on GoldenRepoManager;
    # this repo has no cascade-deletable activated repos in these tests, and
    # this same None-assignment pattern is the established convention for
    # this exact scenario (see test_golden_repo_manager_remove_lock_1843.py).
    manager.activated_repo_manager = None  # type: ignore[assignment]
    return clone_path


def _submit_and_get_worker(manager: GoldenRepoManager, alias: str):
    manager.remove_golden_repo(alias)
    return manager.background_job_manager.submit_job.call_args.kwargs["func"]


class TestCleanupFilesystemPermissionErrorControl:
    """Site 1 (`_cleanup_filesystem`): a genuine PermissionError must still
    fail, still log, and still quarantine."""

    def test_unit_permission_error_still_reports_failure(self, manager, caplog):
        clone_path = _make_populated_dir(manager.golden_repos_dir, "perm-repo")
        assert os.path.exists(clone_path)

        caplog.set_level("WARNING")
        with patch(
            f"{MODULE}.shutil.rmtree",
            side_effect=PermissionError(errno.EACCES, "Permission denied", clone_path),
        ):
            result = manager._cleanup_filesystem(Path(clone_path))

        assert result is False, "A real PermissionError must still be a failure"
        warning_records = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "cleanup incomplete" in r.message.lower()
        ]
        assert warning_records, "A real PermissionError must still be logged"

    def test_worker_permission_error_still_quarantines_and_fails_job(self, manager):
        alias = "perm-remove-repo"
        clone_path = _register_removable_repo(manager, alias)
        worker = _submit_and_get_worker(manager, alias)

        with patch(
            f"{MODULE}.shutil.rmtree",
            side_effect=PermissionError(
                errno.EACCES, "Permission denied", str(clone_path)
            ),
        ):
            with pytest.raises(GitOperationError, match="cleanup incomplete"):
                worker()

        quarantined = list(clone_path.parent.glob(f"{alias}.corrupt-*"))
        assert len(quarantined) == 1, (
            f"A real PermissionError must still quarantine, found: {quarantined}"
        )


class TestRemoveOrphanCloneForRetryPermissionErrorControl:
    """Site 2 (`_remove_orphan_clone_for_retry`, the `add_golden_repo` retry
    path): a genuine PermissionError must still raise with nothing silently
    removed. (This site has no quarantine concept of its own -- the
    directory surviving intact, path AND contents, IS the proof the failure
    was not swallowed.)"""

    def test_permission_error_still_raises_and_leaves_directory_untouched(
        self, manager
    ):
        alias = "perm-orphan"
        clone_path = _make_populated_dir(manager.golden_repos_dir, alias)
        marker_path = os.path.join(clone_path, "marker.txt")
        assert os.path.exists(clone_path)

        with patch(
            f"{MODULE}.shutil.rmtree",
            side_effect=PermissionError(errno.EACCES, "Permission denied", clone_path),
        ):
            with pytest.raises(GitOperationError, match="Failed orphan cleanup"):
                manager._remove_orphan_clone_for_retry(
                    f"https://example.test/{alias}.git", alias
                )

        assert os.path.exists(clone_path), (
            "A real PermissionError must not silently remove the directory"
        )
        with open(marker_path) as fh:
            assert fh.read() == MARKER_CONTENT, (
                "A real PermissionError must not silently alter directory contents"
            )
