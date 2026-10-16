"""Bug #2012 review: an unreadable cancel flag must be loud, not silent --
yet must never stop a legitimately long job.

Reads of the REAL SQLite job store are made to fail by renaming the
background_jobs table away (and back, to prove recovery).
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)

JOB_ID = "long-indexing-job"
MANY_FAILURES = 200


@pytest.fixture
def manager_and_db(tmp_path: Path) -> Iterator[Tuple[BackgroundJobManager, str]]:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "jobs.db")
    DatabaseSchema(db_path).initialize_database()
    manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
    now = datetime.now(timezone.utc)
    manager.jobs[JOB_ID] = BackgroundJob(
        job_id=JOB_ID,
        operation_type="global_repo_refresh",
        status=JobStatus.RUNNING,
        created_at=now,
        started_at=now,
        completed_at=None,
        result=None,
        error=None,
        progress=10,
        username="system",
    )
    try:
        yield manager, db_path
    finally:
        manager.shutdown()


def _rename_jobs_table(db_path: str, src: str, dst: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"ALTER TABLE {src} RENAME TO {dst}")


def _records(caplog: pytest.LogCaptureFixture, level: int) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == level
        and JOB_ID in r.getMessage()
        and "cancel" in r.getMessage()
    ]


def test_failed_cancel_reads_are_logged_rate_limited_and_escalated_once(
    manager_and_db: Tuple[BackgroundJobManager, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, db_path = manager_and_db
    _rename_jobs_table(db_path, "background_jobs", "background_jobs_away")
    caplog.set_level(logging.DEBUG)

    manager._check_db_cancellation(JOB_ID)
    first = _records(caplog, logging.WARNING)
    assert len(first) == 1, "the first unreadable cancel flag must be a WARNING"
    assert first[0].exc_info is not None, "the WARNING must carry the traceback"

    for _ in range(MANY_FAILURES - 1):
        manager._check_db_cancellation(JOB_ID)

    warnings = _records(caplog, logging.WARNING)
    errors = _records(caplog, logging.ERROR)
    assert 1 < len(warnings) < MANY_FAILURES / 2, (
        f"repeated failures must be rate-limited, got {len(warnings)} WARNINGs"
    )
    assert len(errors) == 1, "persistent failure must escalate to ERROR exactly once"
    assert manager.jobs[JOB_ID].cancelled is False, "a read failure is not a cancel"
    assert manager.jobs[JOB_ID].status == JobStatus.RUNNING


def test_successful_read_resets_the_failure_streak(
    manager_and_db: Tuple[BackgroundJobManager, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager, db_path = manager_and_db
    _rename_jobs_table(db_path, "background_jobs", "background_jobs_away")
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        manager._check_db_cancellation(JOB_ID)
    _rename_jobs_table(db_path, "background_jobs_away", "background_jobs")
    manager._check_db_cancellation(JOB_ID)  # succeeds: streak over

    caplog.clear()
    _rename_jobs_table(db_path, "background_jobs", "background_jobs_away")
    manager._check_db_cancellation(JOB_ID)
    assert len(_records(caplog, logging.WARNING)) == 1, (
        "after a successful read, the next failure starts a new streak"
    )
