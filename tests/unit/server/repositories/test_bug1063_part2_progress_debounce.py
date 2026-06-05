"""
Tests for Bug #1063 Part 2: progress_callback debounce in BackgroundJobManager.

The progress_callback is invoked frequently during indexing (every few chunks).
Without debounce, each call hits _persist_jobs() → SQLite write, creating
dozens/hundreds of writes per second during a large repo refresh.

Debounce policy:
- Intermediate ticks within DEBOUNCE_INTERVAL are coalesced: in-memory state
  updated, but _persist_jobs NOT called until the interval elapses.
- Terminal state (COMPLETED / FAILED / CANCELLED) flush to DB immediately,
  regardless of debounce window.
- Cancellation check (_check_db_cancellation) fires on every call (cheap read,
  no write; ensures responsive cancellation latency).

Design notes:
- DEBOUNCE_INTERVAL = 0.5s (progress from a 1-hour index every few seconds
  is fine; 0.5s gives responsive UI without hammering SQLite).
- The debounce is per-job: tracked as a closure variable _last_persist inside
  the progress_callback defined in _execute_job.
"""

import time
import threading
from typing import cast
from unittest.mock import patch

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path) -> BackgroundJobManager:
    db_path = str(tmp_path / "jobs.db")
    return BackgroundJobManager(
        background_jobs_config=BackgroundJobsConfig(max_concurrent_background_jobs=5),
        db_path=db_path,
    )


def _run_job_with_n_ticks(
    mgr: BackgroundJobManager,
    n_ticks: int,
    tick_delay: float = 0.0,
    terminal_success: bool = True,
) -> str:
    """Submit a job whose worker emits n intermediate progress ticks then completes."""

    done = threading.Event()

    def worker(progress_callback=None):
        for i in range(n_ticks):
            if progress_callback:
                progress_callback(i + 1, phase="semantic", detail=f"chunk {i}")
            if tick_delay > 0:
                time.sleep(tick_delay)
        done.set()
        return {"success": True} if terminal_success else {"success": False}

    job_id: str = cast(
        str,
        mgr.submit_job(
            "global_repo_refresh",
            worker,
            submitter_username="system",
            is_admin=True,
            repo_alias="test-repo-global",
        ),
    )

    done.wait(timeout=10.0)
    # Allow a moment for the job completion persist to run
    time.sleep(0.2)
    return job_id


# ===========================================================================
# Part 2A: Rapid ticks within debounce window are coalesced
# ===========================================================================


class TestProgressDebounceCoalescing:
    """Rapid intermediate progress ticks within DEBOUNCE_INTERVAL must not each persist."""

    def test_rapid_ticks_do_not_each_call_persist(self, tmp_path):
        """
        10 rapid ticks fired within a single debounce window should result in
        far fewer _persist_jobs calls than 10. The in-memory state is updated
        each time, but DB writes are batched.
        """
        mgr = _make_manager(tmp_path)

        persist_call_count = [0]
        original_persist = mgr._persist_jobs

        def counting_persist(*args, **kwargs):
            persist_call_count[0] += 1
            return original_persist(*args, **kwargs)

        with patch.object(mgr, "_persist_jobs", side_effect=counting_persist):
            _run_job_with_n_ticks(mgr, n_ticks=10, tick_delay=0.0)

        # With 10 ticks but debounce at 0.5s, we should see far fewer than 10
        # intermediate persist calls (at most ~2 for a 0-tick job, plus 1 for terminal).
        # Conservative check: must be < 10 (proves coalescing happens).
        # We expect roughly: 1 (RUNNING transition) + 0-1 (first tick) + 1 (terminal) = 3
        total = persist_call_count[0]
        # Terminal persist always fires. The debounced intermediate ticks should NOT
        # each fire a persist. If all 10 ticks each fired a persist, that would be 10+
        # and prove debounce is NOT working.
        assert total < 10, (
            f"Expected debounce to coalesce rapid ticks: got {total} persist calls "
            f"for 10 ticks with no delay — debounce is not working."
        )

    def test_in_memory_state_updated_on_every_tick(self, tmp_path):
        """
        Even when debounce suppresses a DB write, the in-memory job.progress
        value must be updated on every tick so get_job() reflects current progress.
        """
        mgr = _make_manager(tmp_path)
        seen_progress = []

        def counting_job(progress_callback=None):
            for pct in [10, 20, 30, 40, 50]:
                if progress_callback:
                    progress_callback(pct, phase="semantic")
                # record in-memory progress after each tick
                with mgr._lock:
                    for job in mgr.jobs.values():
                        seen_progress.append(job.progress)
            return {"success": True}

        job_id = mgr.submit_job(
            "global_repo_refresh",
            counting_job,
            submitter_username="system",
            is_admin=True,
            repo_alias="test-repo-global",
        )

        # Wait for completion
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with mgr._lock:
                if job_id in mgr.jobs and mgr.jobs[job_id].status in (
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.COMPLETED_PARTIAL,
                ):
                    break
            time.sleep(0.05)

        # After each callback, in-memory progress should reflect the tick value.
        # We should see the 5 intermediate values appear (in order, possibly with
        # some interleaved with the initial 10% from RUNNING transition).
        assert 50 in seen_progress or 40 in seen_progress, (
            f"In-memory progress not updated per tick: seen={seen_progress}"
        )


# ===========================================================================
# Part 2B: Terminal state flushes immediately (no debounce delay)
# ===========================================================================


class TestProgressTerminalFlush:
    """Terminal state (COMPLETED/FAILED/CANCELLED) must flush to DB immediately."""

    def test_terminal_completed_persists_immediately(self, tmp_path):
        """
        On job completion, _persist_jobs must be called for the terminal state
        even if the last intermediate tick was within the debounce window.
        """
        mgr = _make_manager(tmp_path)

        terminal_persists = []
        original_persist = mgr._persist_jobs

        def spy_persist(*args, **kwargs):
            result = original_persist(*args, **kwargs)
            # Check if this persist captured a terminal state
            with mgr._lock:
                for jid, job in mgr.jobs.items():
                    if job.status in (
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                        JobStatus.COMPLETED_PARTIAL,
                    ):
                        terminal_persists.append(job.status)
            return result

        with patch.object(mgr, "_persist_jobs", side_effect=spy_persist):
            _run_job_with_n_ticks(mgr, n_ticks=5, tick_delay=0.0)

        # Terminal state must have been persisted
        assert len(terminal_persists) >= 1, (
            "Terminal COMPLETED state was never persisted — debounce is blocking "
            "the terminal flush."
        )

    def test_terminal_failed_persists_immediately(self, tmp_path):
        """Failed jobs must also flush immediately."""
        mgr = _make_manager(tmp_path)

        terminal_persists = []
        original_persist = mgr._persist_jobs

        def spy_persist(*args, **kwargs):
            result = original_persist(*args, **kwargs)
            with mgr._lock:
                for jid, job in mgr.jobs.items():
                    if job.status == JobStatus.FAILED:
                        terminal_persists.append(JobStatus.FAILED)
            return result

        with patch.object(mgr, "_persist_jobs", side_effect=spy_persist):
            _run_job_with_n_ticks(
                mgr, n_ticks=5, tick_delay=0.0, terminal_success=False
            )

        assert len(terminal_persists) >= 1, "Terminal FAILED state was never persisted."


# ===========================================================================
# Part 2C: Cancellation latency preserved (check_db_cancellation every tick)
# ===========================================================================


class TestProgressCancellationLatency:
    """Cancellation detection must fire on every progress tick regardless of debounce."""

    def test_cancellation_check_called_on_every_tick(self, tmp_path):
        """
        _check_db_cancellation must be called on every progress_callback invocation,
        not just when debounce allows a DB persist. This ensures cancel latency
        is bounded by tick frequency, not by DEBOUNCE_INTERVAL.
        """
        mgr = _make_manager(tmp_path)

        cancellation_checks = [0]
        original_check = mgr._check_db_cancellation

        def counting_check(*args, **kwargs):
            cancellation_checks[0] += 1
            return original_check(*args, **kwargs)

        n_ticks = 8

        with patch.object(mgr, "_check_db_cancellation", side_effect=counting_check):
            _run_job_with_n_ticks(mgr, n_ticks=n_ticks, tick_delay=0.0)

        # _check_db_cancellation must be called at least n_ticks times
        # (one per tick, regardless of debounce).
        assert cancellation_checks[0] >= n_ticks, (
            f"Cancellation check was only called {cancellation_checks[0]} times "
            f"for {n_ticks} ticks — debounce must not suppress cancellation checks."
        )


# ===========================================================================
# Part 2D: Ticks spread across debounce windows do persist
# ===========================================================================


class TestProgressDebounceWindowExpiry:
    """Ticks spaced beyond DEBOUNCE_INTERVAL each trigger a persist."""

    def test_slow_ticks_each_persist(self, tmp_path):
        """
        When ticks are separated by > DEBOUNCE_INTERVAL (0.5s), each tick
        should eventually result in a DB persist.

        We use 3 ticks with 0.6s delay between each — each tick lands in
        a fresh debounce window and must persist.
        """
        mgr = _make_manager(tmp_path)

        intermediate_persist_count = [0]
        original_persist = mgr._persist_jobs

        # Track the progress values that triggered a persist
        persisted_progress_values = []

        def spy_persist(*args, **kwargs):
            result = original_persist(*args, **kwargs)
            intermediate_persist_count[0] += 1
            with mgr._lock:
                for job in mgr.jobs.values():
                    persisted_progress_values.append(job.progress)
            return result

        n_ticks = 3
        debounce_gap = 0.6  # > DEBOUNCE_INTERVAL (0.5s)

        with patch.object(mgr, "_persist_jobs", side_effect=spy_persist):
            _run_job_with_n_ticks(mgr, n_ticks=n_ticks, tick_delay=debounce_gap)

        # Each inter-debounce-window tick should persist. We expect at least n_ticks
        # persists (one per tick) plus the terminal persist.
        assert intermediate_persist_count[0] >= n_ticks, (
            f"Expected at least {n_ticks} persists for ticks spaced {debounce_gap}s apart "
            f"(> DEBOUNCE_INTERVAL). Got {intermediate_persist_count[0]}."
        )
