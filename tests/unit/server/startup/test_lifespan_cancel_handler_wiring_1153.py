"""Bug #1153 regression guard: cancelling a dep-map job via cancel_job must stop the worker.

Root cause: BackgroundJobManager.cancel_job marks job.cancelled=True but never calls
DependencyMapService.cancel_running_analysis(), so the dep-map worker keeps running
and the SharedJobSentinel is never released, causing new triggers to 409 forever.

Fix: a cancel-handler registry in BackgroundJobManager keyed by operation_type.
The dep-map operation types ("dependency_map_full", "dependency_map_delta") are
registered at startup to call dep_map_service.cancel_running_analysis().

This module tests:
  A. Source-text guard -- lifespan.py calls register_cancel_handler for dep-map types.
  B. Registry unit tests -- register_cancel_handler / cancel_job invoke mechanics.
     PRODUCTION-ACCURATE: jobs registered via JobTracker.register_job_if_no_conflict
     WITHOUT submit_job, exactly as dep-map does in production.
  C. Isolation test -- non-dep-map running job does NOT invoke dep-map handler.
     Also uses production-accurate JobTracker registration path.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.services.job_tracker import JobTracker
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_BG_JOBS_PATH = (
    _REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "repositories"
    / "background_jobs.py"
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    """Initialised SQLite database path."""
    path = str(tmp_path / "test.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture()
def job_tracker(db_path):
    """Real JobTracker backed by the same SQLite DB."""
    return JobTracker(db_path)


@pytest.fixture()
def manager(db_path, job_tracker):
    """Real BackgroundJobManager with SQLite backend AND a JobTracker, shut down after each test."""
    mgr = BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=4,
        ),
        job_tracker=job_tracker,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_status(
    mgr: BackgroundJobManager, job_id: str, status: JobStatus, timeout: float = 3.0
) -> bool:
    """Poll until job reaches the given status or timeout expires. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with mgr._lock:
            job = mgr.jobs.get(job_id)
        if job and job.status == status:
            return True
        time.sleep(0.05)
    return False


def _wait_for_tracked_status(
    tracker: JobTracker, job_id: str, status: str, timeout: float = 3.0
) -> bool:
    """Poll JobTracker until job reaches given status string."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tracked = tracker.get_job(job_id)
        if tracked is not None and tracked.status == status:
            return True
        time.sleep(0.05)
    return False


def _long_task() -> dict:
    time.sleep(10.0)
    return {"status": "success"}


def _register_depmap_job_via_tracker(
    tracker: JobTracker,
    operation_type: str = "dependency_map_full",
    username: str = "testuser",
) -> str:
    """Register a running dep-map job directly via JobTracker (production path).

    This is exactly how DependencyMapService registers jobs:
    JobTracker.register_job_if_no_conflict(...) WITHOUT submit_job,
    so BackgroundJobManager.jobs stays EMPTY for this job_id.
    """
    job_id = str(uuid.uuid4())
    tracker.register_job(
        job_id=job_id,
        operation_type=operation_type,
        username=username,
        repo_alias=None,
    )
    # Advance to "running" — dep-map calls update_status after starting the thread
    tracker.update_status(job_id, status="running")
    return job_id


# ---------------------------------------------------------------------------
# Part A -- Source-text guards
# ---------------------------------------------------------------------------


class TestLifespanCancelHandlerSourceGuard:
    """lifespan.py must wire dep-map cancel handlers into BackgroundJobManager."""

    def test_register_cancel_handler_called_in_lifespan(self):
        """lifespan.py must call register_cancel_handler at startup."""
        source = _LIFESPAN_PATH.read_text()
        assert "register_cancel_handler" in source, (
            "Bug #1153: lifespan.py does not call register_cancel_handler. "
            "The dep-map cancel handler must be wired at startup."
        )

    def test_lifespan_registers_dependency_map_full(self):
        """lifespan.py must register a handler for 'dependency_map_full'."""
        source = _LIFESPAN_PATH.read_text()
        # Both strings must appear in the same file for the wiring to work
        assert "dependency_map_full" in source, (
            "Bug #1153: 'dependency_map_full' not found in lifespan.py. "
            "The full-analysis cancel handler must be registered."
        )

    def test_lifespan_registers_dependency_map_delta(self):
        """lifespan.py must register a handler for 'dependency_map_delta'."""
        source = _LIFESPAN_PATH.read_text()
        assert "dependency_map_delta" in source, (
            "Bug #1153: 'dependency_map_delta' not found in lifespan.py. "
            "The delta-analysis cancel handler must be registered."
        )

    def test_background_jobs_has_register_cancel_handler_method(self):
        """BackgroundJobManager source must expose register_cancel_handler."""
        source = _BG_JOBS_PATH.read_text()
        assert "register_cancel_handler" in source, (
            "Bug #1153: BackgroundJobManager has no register_cancel_handler method."
        )

    def test_background_jobs_has_cancel_handlers_registry(self):
        """BackgroundJobManager source must have a _cancel_handlers dict."""
        source = _BG_JOBS_PATH.read_text()
        assert "_cancel_handlers" in source, (
            "Bug #1153: BackgroundJobManager has no _cancel_handlers registry dict."
        )


# ---------------------------------------------------------------------------
# Part B -- Registry unit tests (PRODUCTION-ACCURATE: JobTracker path)
# ---------------------------------------------------------------------------


class TestCancelHandlerRegistry:
    """Unit tests for the cancel-handler registry mechanism.

    All tests in this class register dep-map jobs via JobTracker.register_job /
    update_status WITHOUT submit_job.  This is the PRODUCTION PATH used by
    DependencyMapService.  BackgroundJobManager.jobs is EMPTY for these job_ids.
    The cancel handler MUST still be invoked when cancel_job is called.
    """

    def test_register_cancel_handler_stores_callable(self, manager):
        """register_cancel_handler stores the callable keyed by operation_type."""
        cancel_event = threading.Event()

        def handler():
            cancel_event.set()

        manager.register_cancel_handler("dependency_map_full", handler)
        assert "dependency_map_full" in manager._cancel_handlers
        assert manager._cancel_handlers["dependency_map_full"] is handler

    def test_cancel_job_running_depmap_full_invokes_handler(self, manager, job_tracker):
        """cancel_job on a RUNNING dependency_map_full job (JobTracker path) must invoke handler.

        This is the PRIMARY Bug #1153 regression test.

        Production reality: dep-map jobs are registered via JobTracker.register_job_if_no_conflict
        WITHOUT submit_job, so BackgroundJobManager.jobs is EMPTY for these job_ids.
        The prior fix only invoked the handler inside the self.jobs branch — a silent no-op
        for the actual production path.  This test reproduces that failure.
        """
        cancel_event = threading.Event()

        manager.register_cancel_handler("dependency_map_full", cancel_event.set)

        # Register via JobTracker ONLY — no submit_job, so self.jobs stays empty
        job_id = _register_depmap_job_via_tracker(
            job_tracker, operation_type="dependency_map_full"
        )

        # Verify BackgroundJobManager.jobs is empty for this job_id
        with manager._lock:
            assert job_id not in manager.jobs, (
                "Test setup error: job_id appeared in manager.jobs — "
                "it should only be in JobTracker"
            )

        result = manager.cancel_job(job_id, username="testuser", is_admin=True)

        assert result["success"] is True, f"cancel_job returned failure: {result}"
        assert cancel_event.is_set(), (
            "Bug #1153: dep-map cancel handler was NOT called by cancel_job "
            "on the JobTracker path. "
            "The _cancel_event in DependencyMapService was never set, "
            "so the worker thread keeps running and the sentinel is never released."
        )

    def test_cancel_job_running_depmap_delta_invokes_handler(
        self, manager, job_tracker
    ):
        """cancel_job on a RUNNING dependency_map_delta job (JobTracker path) must invoke handler."""
        cancel_event = threading.Event()

        manager.register_cancel_handler("dependency_map_delta", cancel_event.set)

        job_id = _register_depmap_job_via_tracker(
            job_tracker, operation_type="dependency_map_delta"
        )

        with manager._lock:
            assert job_id not in manager.jobs, "Test setup error: job in manager.jobs"

        result = manager.cancel_job(job_id, username="testuser", is_admin=True)

        assert result["success"] is True
        assert cancel_event.is_set(), (
            "Bug #1153: dep-map delta cancel handler was NOT called by cancel_job "
            "on the JobTracker path."
        )

    def test_cancel_job_pending_depmap_does_not_invoke_handler(
        self, manager, job_tracker
    ):
        """cancel_job on a PENDING dep-map job (JobTracker path) must NOT invoke the cancel handler.

        PENDING jobs have not started running, so calling cancel_running_analysis()
        would be wrong — the dep-map lock is not held and there is no worker to stop.
        """
        cancel_event = threading.Event()
        manager.register_cancel_handler("dependency_map_full", cancel_event.set)

        # Register job but leave it PENDING (no update_status("running"))
        job_id = str(uuid.uuid4())
        job_tracker.register_job(
            job_id=job_id,
            operation_type="dependency_map_full",
            username="testuser",
            repo_alias=None,
        )
        # Confirm it is pending in the tracker
        tracked = job_tracker.get_job(job_id)
        assert tracked is not None and tracked.status == "pending"

        # Cancel the pending job
        result = manager.cancel_job(job_id, username="testuser", is_admin=True)

        assert result["success"] is True, f"cancel_job returned failure: {result}"
        assert not cancel_event.is_set(), (
            "cancel_job on a PENDING dep-map job must NOT invoke the cancel handler."
        )

    def test_handler_error_does_not_break_cancel_job(self, manager, job_tracker):
        """A cancel handler that raises must not propagate -- cancel_job still succeeds."""

        def raising_handler():
            raise RuntimeError("simulated handler failure")

        manager.register_cancel_handler("dependency_map_full", raising_handler)

        job_id = _register_depmap_job_via_tracker(
            job_tracker, operation_type="dependency_map_full"
        )

        result = manager.cancel_job(job_id, username="testuser", is_admin=True)
        assert result["success"] is True, (
            "cancel_job must return success=True even when the cancel handler raises. "
            f"Got: {result}"
        )


# ---------------------------------------------------------------------------
# Part C -- Isolation: non-dep-map jobs must NOT trigger dep-map handler
# ---------------------------------------------------------------------------


class TestCancelHandlerIsolation:
    """Non-dep-map running jobs must not invoke the dep-map cancel handler.

    Uses the JobTracker registration path so the test matches production.
    """

    def test_cancel_non_depmap_running_job_does_not_invoke_depmap_handler(
        self, manager, job_tracker
    ):
        """Cancelling an xray_search or other non-dep-map job must NOT trigger dep-map handler."""
        dep_map_cancel_event = threading.Event()

        manager.register_cancel_handler("dependency_map_full", dep_map_cancel_event.set)
        manager.register_cancel_handler(
            "dependency_map_delta", dep_map_cancel_event.set
        )

        # Register an xray_search job via JobTracker (not submit_job)
        job_id = str(uuid.uuid4())
        job_tracker.register_job(
            job_id=job_id,
            operation_type="xray_search",
            username="testuser",
            repo_alias=None,
        )
        job_tracker.update_status(job_id, status="running")

        with manager._lock:
            assert job_id not in manager.jobs, "Test setup error: job in manager.jobs"

        result = manager.cancel_job(job_id, username="testuser", is_admin=True)

        assert result["success"] is True
        assert not dep_map_cancel_event.is_set(), (
            "Bug #1153 fix isolation violation: cancelling an xray_search job "
            "triggered the dep-map cancel handler. The handler lookup must be "
            "keyed by operation_type -- only matching types invoke their handler."
        )
