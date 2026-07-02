"""
Test for Bug #483: Progress regression — brief 0% during composite rebuild.

Root Cause: background_jobs.py has progress_callback(25) that fires BEFORE
the background_worker executes. When the semantic phase starts,
allocator.phase_start("semantic") returns 0 (first phase), creating a
visible 25% -> 0% regression.

Fix: Move progress_callback(25) into the else block so it only fires for
functions WITHOUT progress_callback. Functions that accept progress_callback
manage their own progress via ProgressPhaseAllocator.

Confirmed bug pattern (via direct script):
  Buggy sequence: [0, 10, 25, 0, 50, 100, 100]
  Fixed sequence: [0, 10, 0, 50, 100, 100]
  (25 must not appear before the function's first emission)
"""

import time
import tempfile
from pathlib import Path


from code_indexer.server.repositories.background_jobs import BackgroundJobManager


class TestBug483ProgressRegression:
    """Verify no 25% -> 0% regression for functions that manage their own progress."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"
        self.manager = BackgroundJobManager(storage_path=str(self.job_storage_path))

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        import shutil
        import os

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_no_hardcoded_25_before_function_with_progress_callback(self):
        """
        When a function accepts progress_callback, the hardcoded 25% must NOT
        fire before the function's first progress update.

        The bug produces this sequence: [0, 10, 25, 0, 50, 100, 100]
        where 25 appears before the function's first emission of 0, creating
        a visible 25->0 regression in the UI.

        After the fix, the sequence must NOT contain 25 followed by a lower value.
        Specifically: 25 must not appear in the sequence when the function
        manages its own progress (accepts progress_callback).
        """
        recorded_values = []
        original_persist = self.manager._persist_jobs

        def capturing_persist(job_id=None):
            """Capture the progress value every time it is persisted."""
            if job_id and job_id in self.manager.jobs:
                recorded_values.append(self.manager.jobs[job_id].progress)
            original_persist(job_id=job_id)

        self.manager._persist_jobs = capturing_persist

        def func_with_progress(progress_callback=None):
            """Simulate a ProgressPhaseAllocator-driven function.

            Its first emission is 0 (phase_start of the first phase).
            Before the fix, the manager injects 25 ahead of this, causing
            a visible 25 -> 0 regression in the UI.
            """
            if progress_callback:
                progress_callback(0, phase="semantic", detail="starting...")
                progress_callback(50, phase="semantic", detail="halfway...")
                progress_callback(100, phase="semantic", detail="done")
            return {"success": True}

        job_id = self.manager.submit_job(
            operation_type="test_no_25_before_func",
            func=func_with_progress,
            submitter_username="test_user",
        )

        # Wait for completion
        for _ in range(50):
            status = self.manager.get_job_status(job_id, username="test_user")
            if status and status.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)

        # Assert: 25 must NOT appear in the recorded sequence when a function
        # manages its own progress. The hardcoded 25 in the manager should only
        # fire for functions WITHOUT progress_callback.
        assert 25 not in recorded_values, (
            f"Bug #483 regression: hardcoded 25 fired even though function accepts "
            f"progress_callback. Full sequence: {recorded_values}. "
            f"This means the manager emitted 25 before the function's own first "
            f"emission (0), causing a visible 25->0 regression in the UI."
        )

        # Verify the sequence contains the function's own emissions
        assert 0 in recorded_values, f"Expected 0 in sequence {recorded_values}"
        assert 50 in recorded_values, f"Expected 50 in sequence {recorded_values}"
        assert 100 in recorded_values, f"Expected 100 in sequence {recorded_values}"

        # Job must have completed
        final = self.manager.get_job_status(job_id, username="test_user")
        assert final is not None
        assert final["status"] == "completed", f"Job did not complete: {final}"

    def test_hardcoded_25_fires_for_function_without_progress_callback(self):
        """
        For functions WITHOUT progress_callback, the manager should still
        set progress to 25 (indicating 'running, executing function').

        This ensures we didn't break the non-callback path.
        """
        recorded_values = []
        original_persist = self.manager._persist_jobs

        def capturing_persist(job_id=None):
            if job_id and job_id in self.manager.jobs:
                recorded_values.append(self.manager.jobs[job_id].progress)
            original_persist(job_id=job_id)

        self.manager._persist_jobs = capturing_persist

        def func_without_progress():
            """Function that takes no progress_callback."""
            return {"success": True}

        job_id = self.manager.submit_job(
            operation_type="test_25_for_no_callback",
            func=func_without_progress,
            submitter_username="test_user",
        )

        # Wait for completion
        for _ in range(30):
            status = self.manager.get_job_status(job_id, username="test_user")
            if status and status["status"] == "completed":
                break
            time.sleep(0.1)

        # Progress 25 must appear in the sequence for functions without callback
        assert 25 in recorded_values, (
            f"Expected 25 in sequence for function without progress_callback, "
            f"got {recorded_values}"
        )
