"""Solo (SQLite): a job nobody reassigned or cancelled is never stopped by
its cancel check and runs to completion, whatever node id the tracker
stamps."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, List, Optional

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

POLLS = 20
POLL_SECONDS = 0.05
FINISH_BOUND_SECONDS = 30


def _wait_for(predicate: Any, bound: float) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return bool(predicate())


@pytest.mark.parametrize("node_id", ["solo-node", None])
def test_unreassigned_job_runs_to_completion(tmp_path: Path, node_id: Optional[str]):
    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    manager = BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        job_tracker=JobTracker(db_path, node_id=node_id),
        node_id=node_id,
    )
    answers: List[bool] = []

    def work(progress_callback=None, cancel_check=None):
        for tick in range(POLLS):
            progress_callback(tick)
            answers.append(bool(cancel_check()))
            time.sleep(POLL_SECONDS)
        return {"success": True}

    try:
        job_id = manager.submit_job(
            "global_repo_refresh",
            work,
            submitter_username="system",
            repo_alias="example-repo-global",
        )
        assert _wait_for(
            lambda: job_id not in manager._running_jobs, FINISH_BOUND_SECONDS
        )
        assert answers == [False] * POLLS
        row = BackgroundJobsSqliteBackend(db_path).get_job(job_id)
        assert row is not None and row["status"] == "completed"
    finally:
        manager.shutdown()
