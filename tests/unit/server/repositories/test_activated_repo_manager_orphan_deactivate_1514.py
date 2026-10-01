"""
Tests for Bug #1514 (secondary finding): an activation that fails after
creating the on-disk clone but BEFORE registration (metadata write) leaves
a permanently unreachable orphan directory.

Root cause:
    deactivate_repository() (the front door) raises ActivatedRepoError
    ("not found") immediately whenever _load_metadata() returns None --
    it never checks whether an on-disk directory still exists for that
    (username, user_alias) pair. _do_deactivate_repository() (the
    background-job WORKER) already implements correct orphan cleanup for
    exactly this "no metadata, dir exists" case (Bug #1030's Fix A), but
    that cleanup logic is unreachable through the front door because
    deactivate_repository() refuses to even submit the job.

    This reproduces GitHub issue #1514's secondary finding: "Because the
    job failed BEFORE registering the repository, it never appeared in
    list_repositories, and a subsequent deactivate_repository call
    correctly reported 'not found' ... the on-disk clone directory ...
    is very likely orphaned on disk with no front-door mechanism to
    clean it up."

Fix:
    deactivate_repository() must check on-disk directory existence when
    metadata is None. If the directory exists, submit the background job
    anyway (routing into _do_deactivate_repository's existing orphan
    cleanup branch) instead of raising. Only a request with NEITHER
    metadata NOR an on-disk directory remains a true 404 (Bug #1120's
    security/idempotency invariant, preserved unchanged).
"""

import os
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)


def _make_manager(data_dir: str) -> ActivatedRepoManager:
    """Return a minimal ActivatedRepoManager backed by a temp filesystem dir."""
    golden_repo_manager = MagicMock()
    golden_repo_manager.golden_repos = {}
    background_job_manager = MagicMock()
    background_job_manager.submit_job.return_value = "job-test-1514"
    return ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


@pytest.fixture
def manager():
    temp_dir = tempfile.mkdtemp()
    try:
        yield _make_manager(temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_deactivate_repository_no_metadata_dir_exists_submits_job(manager) -> None:
    """
    RED: an activation that failed BEFORE registration leaves a directory
    with NO metadata. deactivate_repository() must submit the cleanup job
    (not raise "not found") so the orphan is reachable and gets purged by
    _do_deactivate_repository's existing Bug #1030 orphan-cleanup branch.
    """
    username = "alice"
    user_alias = "orphan-activation"
    repo_dir = os.path.join(manager.activated_repos_dir, username, user_alias)

    # Simulate: clone created, but metadata (registration) never written.
    os.makedirs(repo_dir, exist_ok=True)
    with open(os.path.join(repo_dir, "marker.txt"), "w") as f:
        f.write("orphaned clone content")

    # Pre-condition: no registration, but a real on-disk directory.
    assert manager._load_metadata(username, user_alias) is None
    assert os.path.exists(repo_dir)

    # Must NOT raise -- must submit the cleanup job.
    job_id = manager.deactivate_repository(username, user_alias)
    assert job_id == "job-test-1514"


def test_deactivate_repository_truly_nonexistent_still_raises(manager) -> None:
    """
    Security/idempotency invariant preserved: a (username, alias) with
    NEITHER metadata NOR an on-disk directory must still raise
    ActivatedRepoError -- this is the genuine 404 case (Bug #1120).
    """
    username = "dave"
    user_alias = "never-activated"
    repo_dir = os.path.join(manager.activated_repos_dir, username, user_alias)

    assert manager._load_metadata(username, user_alias) is None
    assert not os.path.exists(repo_dir)

    with pytest.raises(ActivatedRepoError, match="not found"):
        manager.deactivate_repository(username, user_alias)
