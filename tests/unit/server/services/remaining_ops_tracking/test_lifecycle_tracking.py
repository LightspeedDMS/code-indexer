"""
AC7: GlobalReposLifecycleManager job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC7: GlobalReposLifecycleManager accepts Optional[JobTracker] parameter
- AC7: startup_reconcile operation type is registered during reconciliation
- AC7: Reconciliation completion transitions to completed
- AC7: Reconciliation failure transitions to failed
- AC7: Tracker=None doesn't break reconciliation
- AC7: Tracker raising exceptions doesn't break reconciliation
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.lifecycle.global_repos_lifecycle import GlobalReposLifecycleManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_manager(tmp_path, job_tracker=None):
    """Create a GlobalReposLifecycleManager with optional job_tracker."""
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    (golden_repos_dir / "aliases").mkdir(exist_ok=True)
    (golden_repos_dir / ".registry").mkdir(exist_ok=True)

    return GlobalReposLifecycleManager(
        str(golden_repos_dir),
        job_tracker=job_tracker,
    )


# ---------------------------------------------------------------------------
# AC7: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestGlobalReposLifecycleManagerConstructor:
    """AC7: GlobalReposLifecycleManager accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self, tmp_path):
        """
        GlobalReposLifecycleManager can be constructed without a job_tracker.

        Given no job_tracker is provided
        When GlobalReposLifecycleManager is instantiated
        Then no exception is raised and _job_tracker is None
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=None)
        assert manager is not None
        assert manager._job_tracker is None

    def test_accepts_job_tracker_instance(self, tmp_path, job_tracker):
        """
        GlobalReposLifecycleManager stores the job_tracker.

        Given a real JobTracker instance
        When GlobalReposLifecycleManager is instantiated with it
        Then _job_tracker is set
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=job_tracker)
        assert manager._job_tracker is job_tracker

    def test_backward_compatible_without_job_tracker(self, tmp_path):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When GlobalReposLifecycleManager is instantiated
        Then no TypeError is raised
        """
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        manager = GlobalReposLifecycleManager(str(golden_repos_dir))
        assert manager is not None


# ---------------------------------------------------------------------------
# AC7: startup_reconcile job registered during reconciliation thread
# ---------------------------------------------------------------------------


class TestStartupReconcileJobRegistration:
    """AC7: startup_reconcile operation type is registered during reconciliation."""

    def test_registers_startup_reconcile_job(self, tmp_path, job_tracker):
        """
        Reconciliation thread registers a startup_reconcile job.

        Given a GlobalReposLifecycleManager with job_tracker
        When reconciliation runs (mocked)
        Then a startup_reconcile job exists in the tracker
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=job_tracker)

        reconcile_done = threading.Event()

        def fake_reconcile():
            reconcile_done.set()

        with patch.object(
            manager.refresh_scheduler, "reconcile_golden_repos", side_effect=fake_reconcile
        ):
            manager.start()
            reconcile_done.wait(timeout=5.0)

        # Allow a brief moment for the thread to register the job
        time.sleep(0.1)

        jobs = job_tracker.query_jobs(operation_type="startup_reconcile")
        assert len(jobs) >= 1

        manager.stop()

    def test_startup_reconcile_job_completes_successfully(self, tmp_path, job_tracker):
        """
        startup_reconcile job transitions to completed after reconciliation.

        Given a GlobalReposLifecycleManager with job_tracker
        When reconcile_golden_repos() succeeds
        Then the startup_reconcile job has completed status
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=job_tracker)

        reconcile_done = threading.Event()

        def fake_reconcile():
            reconcile_done.set()

        with patch.object(
            manager.refresh_scheduler, "reconcile_golden_repos", side_effect=fake_reconcile
        ):
            manager.start()
            reconcile_done.wait(timeout=5.0)

        time.sleep(0.2)  # Wait for job completion

        jobs = job_tracker.query_jobs(operation_type="startup_reconcile", status="completed")
        assert len(jobs) >= 1

        manager.stop()

    def test_startup_reconcile_job_fails_when_reconcile_raises(self, tmp_path, job_tracker):
        """
        startup_reconcile job transitions to failed when reconciliation raises.

        Given a GlobalReposLifecycleManager with job_tracker
        When reconcile_golden_repos() raises an exception
        Then a startup_reconcile job exists with failed status
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=job_tracker)

        reconcile_done = threading.Event()

        def failing_reconcile():
            reconcile_done.set()
            raise RuntimeError("Reconciliation failed")

        with patch.object(
            manager.refresh_scheduler,
            "reconcile_golden_repos",
            side_effect=failing_reconcile,
        ):
            manager.start()
            reconcile_done.wait(timeout=5.0)

        time.sleep(0.2)  # Wait for job failure handling

        jobs = job_tracker.query_jobs(operation_type="startup_reconcile")
        assert len(jobs) >= 1
        failed = [j for j in jobs if j["status"] == "failed"]
        assert len(failed) >= 1

        manager.stop()

    def test_no_job_tracker_does_not_break_reconciliation(self, tmp_path):
        """
        When job_tracker is None, reconciliation proceeds normally.

        Given a GlobalReposLifecycleManager WITHOUT job_tracker
        When start() triggers reconciliation
        Then no exception is raised
        """
        manager = _make_lifecycle_manager(tmp_path, job_tracker=None)

        reconcile_done = threading.Event()

        def fake_reconcile():
            reconcile_done.set()

        with patch.object(
            manager.refresh_scheduler, "reconcile_golden_repos", side_effect=fake_reconcile
        ):
            manager.start()
            reconcile_done.wait(timeout=5.0)

        assert reconcile_done.is_set()
        manager.stop()

    def test_tracker_exception_does_not_break_reconciliation(self, tmp_path):
        """
        When job_tracker raises on register_job, reconciliation still runs.

        Given a job_tracker that raises RuntimeError on register_job
        When start() triggers reconciliation
        Then reconcile_golden_repos() is still called (no exception propagation)
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")
        manager = _make_lifecycle_manager(tmp_path, job_tracker=broken_tracker)

        reconcile_done = threading.Event()

        def fake_reconcile():
            reconcile_done.set()

        with patch.object(
            manager.refresh_scheduler, "reconcile_golden_repos", side_effect=fake_reconcile
        ):
            manager.start()
            reconcile_done.wait(timeout=5.0)

        # Reconciliation must still happen despite tracker failure
        assert reconcile_done.is_set()
        manager.stop()
