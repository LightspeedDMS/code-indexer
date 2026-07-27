"""
Unit tests for BackgroundJobManager progress-debounce coarse-jump bypass
(Bug #1478 part b, Epic #1454 / Story #1461 item 11b).

Root cause: progress_callback's debounce (PROGRESS_DEBOUNCE_INTERVAL) only
persists to the shared DB once per 0.5s window. A fast burst of coarse
progress jumps (e.g. 10->20->30->40 in a sub-millisecond burst) followed by a
long stall meant only the FIRST tick after the debounce window truly
persisted -- the shared DB row could get stuck at an early value while the
in-memory job object (visible only to the node actually running the job)
held the true, later value. Under HAProxy round-robin in a cluster, a poll
routed to a different node reads the stale DB row, producing non-monotonic
progress oscillation on repeated polls of the same job.

These tests prove: (1) a coarse jump (delta >= 10 between consecutive
progress_callback calls) bypasses the debounce and persists immediately, so
the shared DB reflects the burst's final value even while the job is still
stalled; (2) a long stream of fine ticks (delta < 10) is NOT affected by the
bypass and continues to respect the existing debounce cadence; (3) the
persisted DB row never regresses across a mixed coarse/fine progress stream.
"""

import tempfile
import time
import os
import shutil
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

pytestmark = pytest.mark.slow

# Polling cadence/bound for tests that need to observe a background job's
# shared-DB state mid-flight. 200 attempts at 0.05s each gives a generous
# 10s ceiling, tolerant of scheduler jitter under system load, while still
# polling frequently enough to catch mid-execution state transitions.
_POLL_INTERVAL_SECONDS = 0.05
_MAX_POLL_ATTEMPTS = 200


class TestProgressDebounceBypass:
    """Test coarse-jump bypass of the progress-persist debounce (Bug #1478)."""

    def setup_method(self):
        """Setup test environment with SQLite backend."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_coarse_burst_persists_final_value_to_shared_db(self):
        """A fast coarse-jump burst (10->20->30->40) must reach the shared DB
        immediately, even while the job is still stalled -- not merely
        eventually after the job completes.
        """

        def task_with_progress(progress_callback=None):
            if progress_callback:
                progress_callback(20)
                progress_callback(30)
                progress_callback(40)
            # Simulate the job stalling for long enough that the OLD
            # debounce-only code would never flush the burst to the DB.
            time.sleep(2.0)
            return {"status": "success"}

        job_id = self.manager.submit_job(
            "coarse_burst_op", task_with_progress, submitter_username="testuser"
        )

        # Sample the DB WHILE the job is still sleeping (well before the
        # 2.0s stall completes), proving the value hit the DB immediately
        # rather than merely eventually at job completion.
        observed_progress = None
        for _ in range(20):
            time.sleep(0.05)
            db_job = self.manager._sqlite_backend.get_job(job_id)  # type: ignore[union-attr]
            if db_job is not None and db_job.get("progress") == 40:
                observed_progress = db_job.get("progress")
                break

        assert observed_progress == 40, (
            "Shared DB row should reflect the final coarse-burst value (40) "
            "immediately, even while the job is still stalled. Got: "
            f"{observed_progress}"
        )

        # Confirm we sampled this WELL before the 2.0s stall would complete
        # (job must still be running/not yet completed at observation time).
        db_job_mid_stall = self.manager._sqlite_backend.get_job(job_id)  # type: ignore[union-attr]
        assert db_job_mid_stall is not None
        assert db_job_mid_stall.get("status") == "running", (
            "Job should still be running (mid-stall) when progress=40 was "
            f"observed in the DB. Got status={db_job_mid_stall.get('status')}"
        )

        # Let the job finish cleanly so teardown doesn't race shutdown.
        time.sleep(2.5)

    def test_fine_ticks_still_respect_debounce_cadence(self):
        """A long burst of many FINE increments (delta < 10 between
        consecutive calls) must NOT trigger the coarse-jump bypass -- the
        existing 0.5s debounce cadence must still gate nearly all of them.
        """
        total_ticks = 2000

        def task_with_progress(progress_callback=None):
            if progress_callback:
                progress = 10
                for _ in range(total_ticks):
                    progress += 1
                    progress_callback(progress)
            return {"status": "success"}

        # Track persist calls scoped to this job.
        persist_calls = []
        original_persist = self.manager._persist_jobs

        def tracking_persist(job_id=None):
            persist_calls.append(job_id)
            return original_persist(job_id=job_id)

        self.manager._persist_jobs = tracking_persist  # type: ignore[method-assign]

        job_id = self.manager.submit_job(
            "fine_ticks_op", task_with_progress, submitter_username="testuser"
        )

        # Wait for completion.
        for _ in range(40):
            time.sleep(0.05)
            job = self.manager.jobs.get(job_id)
            if job is not None and job.status.value in (
                "completed",
                "failed",
                "cancelled",
            ):
                break

        job_specific_calls = [c for c in persist_calls if c == job_id]

        # The tight loop of 2000 delta==1 ticks completes in well under 0.5s
        # of wall-clock time, so the existing time-based debounce should gate
        # nearly all of them. Allow a small bound (well below the 2000 total
        # ticks) to account for the RUNNING-transition persist, the coarse
        # 25% marker (not applicable here since progress_callback is
        # declared), and the terminal COMPLETED persist.
        assert len(job_specific_calls) < total_ticks / 100, (
            "Fine-tick bypass should not fire: expected persist calls far "
            f"fewer than {total_ticks}, got {len(job_specific_calls)}"
        )

    def test_progress_never_regresses_in_persisted_db_row(self):
        """A mixed coarse/fine progress stream, sampled at multiple points
        during execution, must never show the persisted DB row's progress
        value decreasing.
        """

        def task_with_progress(progress_callback=None):
            if progress_callback:
                sequence = [15, 20, 21, 22, 35, 40, 41, 60, 75, 100]
                for value in sequence:
                    progress_callback(value)
                    time.sleep(0.05)
            return {"status": "success"}

        job_id = self.manager.submit_job(
            "mixed_progress_op", task_with_progress, submitter_username="testuser"
        )

        # Bounded polling loop reading the DB row's own status field.
        # Terminal jobs are evicted from the in-memory self.manager.jobs dict
        # on successful persist (see terminal-persist-eviction behavior), so
        # polling in-memory status to detect completion is unreliable -- the
        # DB row's "status" column is the only durable completion signal.
        samples = []
        for _ in range(_MAX_POLL_ATTEMPTS):
            time.sleep(_POLL_INTERVAL_SECONDS)
            db_job = self.manager._sqlite_backend.get_job(job_id)  # type: ignore[union-attr]
            if db_job is not None:
                samples.append(db_job.get("progress"))
                if db_job.get("status") in ("completed", "failed", "cancelled"):
                    break

        # Filter out None values (job row not yet created) while preserving
        # order, then assert monotonic non-decrease.
        numeric_samples = [s for s in samples if s is not None]
        assert len(numeric_samples) >= 2, (
            "Should have collected multiple progress samples during "
            f"execution. Got: {samples}"
        )
        for earlier, later in zip(numeric_samples, numeric_samples[1:]):
            assert later >= earlier, (
                f"Persisted DB progress must never regress. Samples: {numeric_samples}"
            )

        assert numeric_samples[-1] == 100, (
            f"Final persisted progress should reflect completion (100). "
            f"Got: {numeric_samples[-1]}"
        )
