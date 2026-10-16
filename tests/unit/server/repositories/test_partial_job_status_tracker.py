"""A BackgroundJobManager job whose result carries ``partial: True`` ends
``completed_partial`` everywhere: the stored row, ``get_job_status`` (the
``GET /api/jobs/{id}`` source), the JobTracker and its dashboard list.

BackgroundJobManager persists ``completed_partial`` and then notifies the
real JobTracker, whose completion write must not replace it with
``completed`` in the same ``background_jobs`` row. The PostgreSQL mirror is
tests/unit/server/storage/postgres/test_partial_job_status_tracker_postgres.py.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema

PARTIAL_RESULT: Dict[str, object] = {
    "success": True,
    "message": "Refresh complete",
    "partial": True,
    "failed_files": 1,
}


def _partial_refresh() -> Dict[str, object]:
    return dict(PARTIAL_RESULT)


def run_partial_job_scenario(
    bgm: BackgroundJobManager,
    tracker: JobTracker,
    stored_status: Callable[[str], str],
) -> None:
    job_id = bgm.submit_job(
        "refresh_golden_repo",
        _partial_refresh,
        submitter_username="admin",
        is_admin=True,
        repo_alias="example-repo-global",
    )
    # BGM evicts a terminal job from memory only after notifying the
    # tracker, so the tracker's completion write has happened by then.
    deadline = time.monotonic() + 30
    while job_id in bgm.jobs:
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.02)

    assert stored_status(job_id) == "completed_partial"
    status = bgm.get_job_status(job_id, "admin", is_admin=True)
    assert status is not None and status["status"] == "completed_partial"
    tracked = tracker.get_job(job_id)
    assert tracked is not None and tracked.status == "completed_partial"
    assert tracked.result == PARTIAL_RESULT
    listed = [j for j in tracker.get_recent_jobs() if j["job_id"] == job_id]
    assert [j["status"] for j in listed] == ["completed_partial"]
    # Dashboard card and the no-tracker dashboard list: a partial completion
    # is still a completion, never dropped.
    assert bgm.get_job_stats_with_filter("24h")["completed"] == 1
    fallback = [
        j for j in bgm.get_recent_jobs_with_filter("24h") if j["job_id"] == job_id
    ]
    assert [j["status"] for j in fallback] == ["completed_partial"]


def test_partial_result_job_ends_completed_partial_sqlite(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    tracker = JobTracker(db_path)
    bgm = BackgroundJobManager(use_sqlite=True, db_path=db_path, job_tracker=tracker)

    def stored_status(job_id: str) -> str:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM background_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        return str(row[0])

    try:
        run_partial_job_scenario(bgm, tracker, stored_status)
    finally:
        bgm.shutdown()
