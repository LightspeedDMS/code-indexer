"""
Bug #1950 Part 3: BackgroundJobManager._execute_job()'s generic exception
handler must not classify a SIGTERM/shutdown-triggered failure the same as
a genuine one. See test_restart_interrupted_jobs_health_1950.py for the
full root-cause writeup (kept there to avoid repeating it in every split
file).

The RuntimeError text below is the EXACT production error text observed
in Bug #1950 (raised by global_repos/refresh_scheduler.py's
_run_popen_c() when a `cidx index` subprocess is killed by SIGTERM during
a server shutdown, before BackgroundJobManager.shutdown() itself ever
gets a chance to flag the job cancelled).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Generator

import pytest

if TYPE_CHECKING:
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJob,
        BackgroundJobManager,
    )

_DEFAULT_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
_TEST_USERNAME = "test_user"
_MAX_CONCURRENT_JOBS = 10


def _run_job_and_wait(
    manager: "BackgroundJobManager", func, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> "BackgroundJob":
    """Submit func as a background job on an in-memory (no SQLite) manager,
    wait for it to reach a terminal status, return the BackgroundJob.

    Fails the test (via pytest.fail) if the job is still PENDING/RUNNING
    when the timeout expires, rather than silently returning a
    non-terminal job that a status-inequality check could pass against by
    accident.
    """
    from code_indexer.server.repositories.background_jobs import JobStatus

    job_id = manager.submit_job(
        operation_type="global_repo_refresh",
        func=func,
        submitter_username=_TEST_USERNAME,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.jobs.get(job_id)
        if job and job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return job
        time.sleep(_POLL_INTERVAL_SECONDS)

    job = manager.jobs.get(job_id)
    current_status = job.status.value if job is not None else "MISSING"
    pytest.fail(
        f"Job {job_id} did not reach a terminal status within {timeout}s "
        f"(last observed status: {current_status})"
    )


@pytest.fixture
def in_memory_manager() -> Generator["BackgroundJobManager", None, None]:
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    manager = BackgroundJobManager(
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
        ),
    )
    yield manager
    manager.shutdown()


def test_shutdown_interruption_message_is_not_classified_as_failed(
    in_memory_manager: "BackgroundJobManager",
) -> None:
    """Mirrors the exact production error text observed in Bug #1950:
    'Indexing interrupted by server shutdown for typescript-global'."""
    from code_indexer.server.repositories.background_jobs import JobStatus

    def interrupted_by_shutdown():
        raise RuntimeError(
            "Indexing interrupted by server shutdown for typescript-global"
        )

    job = _run_job_and_wait(in_memory_manager, interrupted_by_shutdown)
    assert job.status not in (
        JobStatus.PENDING,
        JobStatus.RUNNING,
    ), f"Job must have reached a terminal status, got {job.status}"
    assert job.status.value != "failed", (
        "Bug #1950: a job killed by an orderly shutdown must not be "
        "classified the same as a genuine failure."
    )


def test_genuine_exception_still_classified_as_failed(
    in_memory_manager: "BackgroundJobManager",
) -> None:
    """Regression guard: an ordinary exception (nothing to do with a
    restart) must still produce a real 'failed' status."""

    def genuinely_broken():
        raise RuntimeError("Git clone failed: permission denied")

    job = _run_job_and_wait(in_memory_manager, genuinely_broken)
    assert job.status.value == "failed", (
        "A genuine failure must still be classified 'failed' so /health "
        "still reports degraded for it."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
