"""v10.4.6 tests for Obs 3.2: is_admin removed from public job-result responses.

v10.4.5 added a docstring explaining BackgroundJob.is_admin is a job-priority scheduling
flag (NOT the submitter's user role), but field testers continued to misread it because
they read response dicts, not source code.  Removing it from the public projection
eliminates the confusion at the source.

Three tests as specified in the v10.4.6 add-on task:

1. test_get_job_status_response_does_not_include_is_admin
   SQLite-backed BJM: completed jobs are evicted from memory, forcing get_job_status
   to return db_job from _row_to_dict() — the actual leak point.

2. test_list_jobs_sqlite_merge_does_not_include_is_admin
   SQLite DB-merge path of list_jobs also must not expose is_admin.

3. test_internal_is_admin_field_still_works_for_priority
   Regression guard: BackgroundJob.is_admin still exists on the dataclass (verified
   via hasattr) and cancel_job ownership-bypass still works.
"""

from __future__ import annotations

import tempfile
import time
import threading
from pathlib import Path
from typing import Any, Dict, Generator

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager

_COMPLETION_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.05
_JOB_START_DELAY_S = 0.1


def _noop_job(progress_callback: Any) -> Dict[str, Any]:
    return {"status": "done"}


@pytest.fixture()
def sqlite_bjm_with_evicted_job() -> Generator[Dict[str, Any], None, None]:
    """Provide a SQLite-backed BJM whose only job has completed and been evicted.

    Yields a dict with keys: bjm, job_id, username, tmpdir.
    The job is guaranteed to no longer be in bjm.jobs when the test body runs.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "jobs.db")
        # Initialize schema (creates background_jobs table) before BJM start.
        # Pattern from tests/unit/server/repositories/test_orphaned_job_cleanup.py.
        DatabaseSchema(db_path).initialize_database()
        bjm = BackgroundJobManager(use_sqlite=True, db_path=db_path)
        username = "testuser@example.com"

        job_id = bjm.submit_job(
            operation_type="xray_search",
            func=_noop_job,
            submitter_username=username,
            repo_alias="test-repo-global",
        )

        # Wait until the completed job is evicted from memory (SQLite-mode eviction)
        deadline = time.monotonic() + _COMPLETION_TIMEOUT_S
        while time.monotonic() < deadline:
            with bjm._lock:
                if job_id not in bjm.jobs:
                    break
            time.sleep(_POLL_INTERVAL_S)
        else:
            pytest.fail(f"Job {job_id} not evicted within {_COMPLETION_TIMEOUT_S}s")

        yield {"bjm": bjm, "job_id": job_id, "username": username}


# ---------------------------------------------------------------------------
# Test 1 — get_job_status SQLite fallback must not expose is_admin
# ---------------------------------------------------------------------------


def test_get_job_status_response_does_not_include_is_admin(
    sqlite_bjm_with_evicted_job: Dict[str, Any],
) -> None:
    """SQLite fallback path of get_job_status must not expose 'is_admin'.

    With a SQLite-backed BJM, completed jobs are evicted from memory.
    get_job_status then falls back to returning db_job from _row_to_dict(),
    which is the actual leak point where is_admin was exposed (Obs 3.2).
    """
    bjm = sqlite_bjm_with_evicted_job["bjm"]
    job_id = sqlite_bjm_with_evicted_job["job_id"]
    username = sqlite_bjm_with_evicted_job["username"]

    with bjm._lock:
        assert job_id not in bjm.jobs, "Job must be evicted before this test is valid"

    status = bjm.get_job_status(job_id, username)

    assert status is not None, "Job must be found in SQLite after eviction"
    assert status.get("status") == "completed"
    assert "is_admin" not in status, (
        f"'is_admin' must NOT appear in get_job_status SQLite fallback response. "
        f"Got keys: {list(status.keys())}"
    )


# ---------------------------------------------------------------------------
# Test 2 — list_jobs SQLite DB-merge path must not expose is_admin
# ---------------------------------------------------------------------------


def test_list_jobs_sqlite_merge_does_not_include_is_admin(
    sqlite_bjm_with_evicted_job: Dict[str, Any],
) -> None:
    """list_jobs() SQLite DB-merge path must not expose 'is_admin' in job dicts.

    With a SQLite-backed BJM, list_jobs fetches db_jobs from the DB (which carry
    is_admin via _row_to_dict), then merges with in-memory jobs.  DB-origin dicts
    not overridden by in-memory entries pass through with is_admin intact unless
    explicitly stripped.  This test verifies the stripping is applied.
    """
    bjm = sqlite_bjm_with_evicted_job["bjm"]
    username = sqlite_bjm_with_evicted_job["username"]

    result = bjm.list_jobs(username=username, limit=10)
    job_list = result.get("jobs", [])

    assert len(job_list) > 0, "Expected at least one job from SQLite"

    for job_dict in job_list:
        assert "is_admin" not in job_dict, (
            f"'is_admin' must NOT appear in any list_jobs SQLite-path job dict. "
            f"Found in job {job_dict.get('job_id')}, keys: {list(job_dict.keys())}"
        )


# ---------------------------------------------------------------------------
# Test 3 — internal is_admin field and cancel ownership-bypass still work
# ---------------------------------------------------------------------------


def test_internal_is_admin_field_still_works_for_priority() -> None:
    """BackgroundJob.is_admin still exists and cancel ownership-bypass still works.

    Removing is_admin from the public response projection must not break the
    internal scheduling/ownership-bypass logic used by cancel_job.
    The test explicitly verifies hasattr(job, 'is_admin') on the live dataclass object.
    """
    from code_indexer.server.utils.config_manager import ServerResourceConfig

    bjm = BackgroundJobManager(resource_config=ServerResourceConfig())
    owner_username = "owner@example.com"
    admin_username = "sysadmin@example.com"

    barrier = threading.Event()

    def _slow_job(progress_callback: Any) -> Dict[str, Any]:
        barrier.wait(timeout=_COMPLETION_TIMEOUT_S)
        return {"status": "done"}

    job_id = bjm.submit_job(
        operation_type="slow_op",
        func=_slow_job,
        submitter_username=owner_username,
        repo_alias="server",
    )

    time.sleep(_JOB_START_DELAY_S)

    # Verify internal field exists on the live in-memory dataclass object
    with bjm._lock:
        job = bjm.jobs.get(job_id)
    assert job is not None, "Job must be in memory before cancel"
    assert hasattr(job, "is_admin"), (
        "BackgroundJob.is_admin field must still exist on the dataclass"
    )
    assert job.is_admin is False, (
        "job.is_admin defaults to False when not set by the handler"
    )

    # Ownership-bypass: admin can cancel another user's job via is_admin=True
    result = bjm.cancel_job(job_id=job_id, username=admin_username, is_admin=True)
    barrier.set()

    assert result.get("success") is True, (
        f"cancel_job with is_admin=True must succeed for another user's job. Got: {result}"
    )
