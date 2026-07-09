"""
Tests for Bug #1063 Part 3: Bounded worker pool for BackgroundJobManager.

Problem: the current design spawns a new thread per submit_job() call.
All threads block on a semaphore waiting for a slot.  With N submits:
  - N threads are created
  - P threads run (P = max_concurrent_background_jobs)
  - N-P threads block indefinitely on semaphore.acquire()

This wastes OS resources: each blocking thread holds a stack (~1MB), a TID,
and a kernel scheduler slot, contributing nothing while it waits.

Fix: bounded worker pool.  Submit_job() enqueues work items.  A fixed pool of
P worker threads pulls from the queue.  N submits → only P threads, not N.

Cancel semantics preserved:
- PENDING (queued, not started): cancelled immediately, never starts.
- RUNNING (worker picked it up): cancellation flag set, child process killed.
"""

import threading
import time
from typing import List, cast

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path, pool_size: int = 2) -> BackgroundJobManager:
    db_path = str(tmp_path / "jobs.db")
    return BackgroundJobManager(
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=pool_size
        ),
        db_path=db_path,
    )


def _submit_blocking_job(
    mgr: BackgroundJobManager,
    started_event: threading.Event,
    release_event: threading.Event,
    alias: str = "repo-global",
    idx: int = 0,
) -> str:
    """Submit a job that signals start then blocks until released."""

    def worker():
        started_event.set()
        release_event.wait(timeout=10.0)
        return {"success": True}

    return cast(
        str,
        mgr.submit_job(
            "global_repo_refresh",
            worker,
            submitter_username="system",
            is_admin=True,
            repo_alias=f"{alias}-{idx}",
        ),
    )


def _wait_for_status(mgr, job_id, target_statuses, timeout=5.0):
    """Poll until a job reaches one of target_statuses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with mgr._lock:
            if job_id in mgr.jobs:
                if mgr.jobs[job_id].status in target_statuses:
                    return mgr.jobs[job_id].status
        time.sleep(0.05)
    with mgr._lock:
        return mgr.jobs.get(job_id, None) and mgr.jobs[job_id].status


# ===========================================================================
# Part 3A: N submits → only pool_size threads (not N threads)
# ===========================================================================


class TestBoundedThreadCount:
    """Only pool_size worker threads should be alive regardless of N submits."""

    def test_n_submits_create_at_most_pool_size_threads(self, tmp_path):
        """
        With pool_size=2, submitting 6 jobs should result in at most pool_size+1
        threads owned by the manager (pool workers), not 6 spawned threads.

        We measure the delta in active threads before and after submission.
        The delta must be <= pool_size + 1 (pool workers + possible dispatcher).

        This fails with the old per-submit Thread() model (delta == N).
        """
        pool_size = 2
        mgr = _make_manager(tmp_path, pool_size=pool_size)

        started_events = [threading.Event() for _ in range(pool_size)]

        # Count threads before
        threads_before = threading.active_count()

        release_events_per_job: List[threading.Event] = []
        job_ids = []

        # Submit pool_size jobs that block (fill all workers)
        for i in range(pool_size):
            rel = threading.Event()
            release_events_per_job.append(rel)
            jid = _submit_blocking_job(mgr, started_events[i], rel, idx=i)
            job_ids.append(jid)

        # Wait for both workers to start (confirms pool is fully utilized)
        for ev in started_events:
            ev.wait(timeout=3.0)

        # Submit N-pool_size more jobs (they will queue as PENDING, not start)
        extra_jobs = 4
        for i in range(extra_jobs):
            rel = threading.Event()
            release_events_per_job.append(rel)
            jid = mgr.submit_job(
                "global_repo_refresh",
                lambda: {"success": True},
                submitter_username="system",
                is_admin=True,
                repo_alias=f"queued-repo-{i}-global",
            )
            job_ids.append(jid)

        # Allow a moment for threads to settle
        time.sleep(0.1)
        threads_after = threading.active_count()
        thread_delta = threads_after - threads_before

        # Release all running jobs to allow cleanup
        for rel in release_events_per_job:
            rel.set()

        mgr.shutdown()

        # Critical assertion: thread delta must be bounded by pool size, not N.
        # Old model: delta == pool_size + extra_jobs (one thread per submit).
        # New model: delta == pool_size (fixed pool) + 0-1 for dispatcher/queue.
        assert thread_delta <= pool_size + 2, (
            f"Too many threads created: {thread_delta} extra threads for "
            f"pool_size={pool_size} with {pool_size + extra_jobs} submits. "
            f"Expected at most {pool_size + 2}. "
            f"The bounded worker pool must not spawn per-submit threads."
        )

    def test_pending_jobs_do_not_create_threads(self, tmp_path):
        """
        Jobs waiting in the PENDING queue must not have a running OS thread.

        Submit pool_size blocking jobs to fill all workers, then submit 1 more.
        The extra job must be PENDING (no thread) until a worker becomes free.
        """
        pool_size = 2
        mgr = _make_manager(tmp_path, pool_size=pool_size)

        started = [threading.Event() for _ in range(pool_size)]
        blockers = [threading.Event() for _ in range(pool_size)]

        job_ids = []
        for i in range(pool_size):
            jid = _submit_blocking_job(mgr, started[i], blockers[i], idx=i)
            job_ids.append(jid)

        # Wait for both to start
        for ev in started:
            ev.wait(timeout=3.0)

        threads_at_full_pool = threading.active_count()

        # Submit one more (must queue)
        extra_jid = mgr.submit_job(
            "global_repo_refresh",
            lambda: {"success": True},
            submitter_username="system",
            is_admin=True,
            repo_alias="pending-only-global",
        )
        time.sleep(0.1)

        threads_after_queue = threading.active_count()

        # Verify: queuing a job must NOT create a new thread
        assert threads_after_queue == threads_at_full_pool, (
            f"Queuing a PENDING job created a new thread! "
            f"Threads before queue: {threads_at_full_pool}, after: {threads_after_queue}. "
            f"PENDING jobs must not spawn threads."
        )

        # Verify: extra job is PENDING
        status = _wait_for_status(mgr, extra_jid, [JobStatus.PENDING], timeout=1.0)
        assert status == JobStatus.PENDING, f"Expected PENDING, got {status}"

        # Cleanup
        for bl in blockers:
            bl.set()
        mgr.shutdown()


# ===========================================================================
# Part 3B: PENDING job cancel → never starts
# ===========================================================================


class TestPendingJobCancelNeverStarts:
    """A PENDING (queued) job that is cancelled must never start execution."""

    def test_cancelled_pending_job_never_runs(self, tmp_path):
        """
        1. Fill all worker slots with blocking jobs.
        2. Submit one more (goes PENDING).
        3. Cancel the PENDING job.
        4. Release the blocking jobs.
        5. Verify the cancelled job never ran (status=CANCELLED, no side effects).
        """
        pool_size = 1
        mgr = _make_manager(tmp_path, pool_size=pool_size)

        blocker_started = threading.Event()
        blocker_release = threading.Event()

        # Fill the single slot (return value unused — we only care the job is running)
        _ = _submit_blocking_job(mgr, blocker_started, blocker_release, idx=0)
        blocker_started.wait(timeout=3.0)

        ran = threading.Event()

        def should_not_run():
            ran.set()
            return {"success": True}

        # Submit a job that should queue as PENDING
        pending_id = mgr.submit_job(
            "global_repo_refresh",
            should_not_run,
            submitter_username="system",
            is_admin=True,
            repo_alias="pending-cancel-global",
        )

        # Wait for it to be PENDING
        status = _wait_for_status(mgr, pending_id, [JobStatus.PENDING], timeout=2.0)
        assert status == JobStatus.PENDING, f"Expected PENDING, got {status}"

        # Cancel the PENDING job
        cancel_result = mgr.cancel_job(pending_id, username="system", is_admin=True)
        assert cancel_result.get("success") is True, f"Cancel failed: {cancel_result}"

        # Release the blocker so the worker becomes free
        blocker_release.set()

        # Wait long enough for the worker to potentially pick up the cancelled job
        time.sleep(0.5)

        # The cancelled job must NEVER have run
        assert not ran.is_set(), (
            "Cancelled PENDING job executed! A cancelled PENDING job must be "
            "dropped from the queue and never start."
        )

        # Final status must be CANCELLED
        final_status = _wait_for_status(
            mgr,
            pending_id,
            [JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED],
            timeout=2.0,
        )
        assert final_status == JobStatus.CANCELLED, (
            f"Expected CANCELLED, got {final_status}"
        )

        mgr.shutdown()


# ===========================================================================
# Part 3C: RUNNING job cancel → child process terminated
# ===========================================================================


class TestRunningJobCancelTerminatesChild:
    """Cancelling a RUNNING job must set cancelled flag and terminate any child processes."""

    def test_cancel_running_job_sets_cancelled_status(self, tmp_path):
        """
        Cancel a RUNNING job and verify it transitions to CANCELLED.

        This tests the existing cancel path (flag-based cancellation via
        progress_callback polling). The bounded pool must not break this path.
        """
        pool_size = 2
        mgr = _make_manager(tmp_path, pool_size=pool_size)

        started = threading.Event()
        release = threading.Event()

        def long_runner():
            started.set()
            release.wait(timeout=10.0)
            return {"success": True}

        job_id = mgr.submit_job(
            "global_repo_refresh",
            long_runner,
            submitter_username="system",
            is_admin=True,
            repo_alias="running-cancel-global",
        )

        started.wait(timeout=3.0)

        # Confirm RUNNING
        status = _wait_for_status(mgr, job_id, [JobStatus.RUNNING], timeout=2.0)
        assert status == JobStatus.RUNNING, f"Expected RUNNING, got {status}"

        # Cancel the running job
        cancel_result = mgr.cancel_job(job_id, username="system", is_admin=True)
        assert cancel_result.get("success") is True

        # Release the blocker so the job can check cancellation and exit
        release.set()

        # Verify eventual CANCELLED state
        final_status = _wait_for_status(
            mgr,
            job_id,
            [JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED],
            timeout=5.0,
        )
        assert final_status == JobStatus.CANCELLED, (
            f"Expected CANCELLED after cancel_job(), got {final_status}"
        )

        mgr.shutdown()

    def test_slot_freed_after_cancel_allows_next_job(self, tmp_path):
        """
        After a running job is cancelled, the freed worker slot must allow
        the next queued job to start.
        """
        pool_size = 1
        mgr = _make_manager(tmp_path, pool_size=pool_size)

        started = threading.Event()
        release = threading.Event()
        next_ran = threading.Event()

        def long_runner():
            started.set()
            release.wait(timeout=10.0)
            return {"success": True}

        def next_job():
            next_ran.set()
            return {"success": True}

        # Fill the single slot
        job1_id = mgr.submit_job(
            "global_repo_refresh",
            long_runner,
            submitter_username="system",
            is_admin=True,
            repo_alias="running-slot-global",
        )
        started.wait(timeout=3.0)

        # Queue a second job (return value unused — we only care it was submitted)
        _ = mgr.submit_job(
            "global_repo_refresh",
            next_job,
            submitter_username="system",
            is_admin=True,
            repo_alias="queued-next-global",
        )
        time.sleep(0.1)

        # Cancel the running job (releases the slot)
        mgr.cancel_job(job1_id, username="system", is_admin=True)
        release.set()

        # The queued job should now run
        assert next_ran.wait(timeout=5.0), (
            "Job2 never ran after job1 was cancelled — slot was not freed."
        )

        mgr.shutdown()
