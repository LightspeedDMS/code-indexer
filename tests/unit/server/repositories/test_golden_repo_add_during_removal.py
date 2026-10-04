"""
Re-adding a golden repo alias while its removal job is still running.

remove_golden_repo's background job deletes the registry row FIRST
(Bug #1317) and the on-disk clone LAST. Between those two steps the alias
looks free: add_golden_repo's "already exists" check passes, its orphan
cleanup deletes the directory the removal is about to delete, it copies a
fresh clone into golden_repos_dir/{alias} -- and then the still-running
removal job rmtree's that fresh clone (observed in the e2e Phase 3 gate:
"Failed to clone repository: ... [Errno 2] No such file or directory:
.../golden-repos/ret1134").

The invariant: an alias whose removal job is pending or running cannot be
added again until that job finishes.

Uses the REAL GoldenRepoManager, REAL SQLite schema and a REAL JobTracker
on the same database; only the BackgroundJobManager (thread dispatch) is a
test double, so the add submission is observable.
"""

import tempfile
from typing import Iterator, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoError,
    GoldenRepoManager,
)
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema

_ALIAS = "example-repo"
_REPO_URL = "https://example.com/example/example-repo.git"


@pytest.fixture
def env() -> Iterator[Tuple[GoldenRepoManager, JobTracker]]:
    with tempfile.TemporaryDirectory() as data_dir:
        mgr = GoldenRepoManager(data_dir=data_dir)
        DatabaseSchema(mgr.db_path).initialize_database()
        bjm = MagicMock(spec=BackgroundJobManager)
        bjm.submit_job.return_value = "add-job-id"
        mgr.background_job_manager = bjm
        tracker = JobTracker(db_path=mgr.db_path)
        mgr.job_tracker = tracker
        yield mgr, tracker


def _start_removal_job(tracker: JobTracker) -> str:
    """Register the removal job exactly as BackgroundJobManager.submit_job
    does for remove_golden_repo (repo-scoped, cluster-atomic gate)."""
    job_id = "remove-job-id"
    tracker.register_job_if_no_conflict(
        job_id=job_id,
        operation_type="remove_golden_repo",
        username="example-admin",
        repo_alias=_ALIAS,
        is_admin=True,
    )
    tracker.update_status(job_id, status="running")
    return job_id


def _add(mgr: GoldenRepoManager) -> str:
    with patch.object(mgr, "_validate_git_repository", return_value=True):
        return mgr.add_golden_repo(
            repo_url=_REPO_URL,
            alias=_ALIAS,
            submitter_username="example-admin",
        )


def test_add_rejected_while_removal_job_active(
    env: Tuple[GoldenRepoManager, JobTracker],
) -> None:
    """The registry row is already gone (removal deletes it first) but the
    removal job is still running: the add must be refused, not submitted."""
    manager, tracker = env
    removal_job_id = _start_removal_job(tracker)
    assert manager.get_golden_repo(_ALIAS) is None  # alias looks free

    with pytest.raises(GoldenRepoError) as excinfo:
        _add(manager)

    assert removal_job_id in str(excinfo.value)
    assert "being removed" in str(excinfo.value)
    manager.background_job_manager.submit_job.assert_not_called()  # type: ignore[attr-defined]


def test_add_allowed_after_removal_job_completed(
    env: Tuple[GoldenRepoManager, JobTracker],
) -> None:
    """Control: once the removal job has finished, the alias can be re-added."""
    manager, tracker = env
    removal_job_id = _start_removal_job(tracker)
    tracker.complete_job(removal_job_id)

    assert _add(manager) == "add-job-id"
    manager.background_job_manager.submit_job.assert_called_once()  # type: ignore[attr-defined]
