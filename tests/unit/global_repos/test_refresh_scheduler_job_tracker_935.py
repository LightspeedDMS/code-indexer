"""
Unit tests for Bug #935 fix: RefreshScheduler registers in-flight refresh jobs
with JobTracker so the drain endpoint knows they exist.

Tests verify:
1. RefreshScheduler accepts and stores job_tracker parameter (defaults to None in CLI mode).
2. When a refresh starts, register_job is called BEFORE update_status(running) — verified via
   mock_calls ordering so the sequence cannot silently reverse.
3. When a refresh ends (success), complete_job is called and fail_job is not.
4. When a refresh ends with an exception, fail_job is called in try/finally.
5. When no job_tracker is configured (CLI mode), _execute_refresh works unchanged.
6. JobTracker exposes get_running_jobs_count() / get_queued_jobs_count() for
   compatibility with the MaintenanceState tracker interface.
"""

import sqlite3
from unittest.mock import MagicMock, patch, call

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager


# ---------------------------------------------------------------------------
# Shared scheduler fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def mock_job_tracker():
    """Minimal mock of JobTracker with the methods called by RefreshScheduler."""
    tracker = MagicMock()
    tracker.register_job = MagicMock(return_value=MagicMock())
    tracker.update_status = MagicMock()
    tracker.complete_job = MagicMock()
    tracker.fail_job = MagicMock()
    return tracker


def _make_scheduler(
    golden_repos_dir,
    config_mgr,
    query_tracker,
    cleanup_manager,
    job_tracker=None,
    registry=None,
):
    """Helper: build a RefreshScheduler with an optional job_tracker."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        job_tracker=job_tracker,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Shared JobTracker DB fixture
# ---------------------------------------------------------------------------

_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        operation_type TEXT,
        status TEXT,
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER DEFAULT 0,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT
    )
"""


@pytest.fixture
def job_tracker_db(tmp_path):
    """Create an initialized JobTracker backed by a real SQLite DB."""
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_BACKGROUND_JOBS_DDL)
    conn.commit()
    conn.close()

    from code_indexer.server.services.job_tracker import JobTracker

    return JobTracker(db_path)


# ---------------------------------------------------------------------------
# AC1: RefreshScheduler accepts and stores job_tracker parameter
# ---------------------------------------------------------------------------


class TestRefreshSchedulerAcceptsJobTracker:
    def test_accepts_job_tracker_parameter(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_job_tracker,
    ):
        """RefreshScheduler.__init__ must accept job_tracker and store it."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=mock_job_tracker,
        )
        assert scheduler._job_tracker is mock_job_tracker

    def test_job_tracker_defaults_to_none(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """job_tracker must default to None (CLI mode, no DB required)."""
        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager
        )
        assert scheduler._job_tracker is None


# ---------------------------------------------------------------------------
# AC2: Job registered with running status at refresh start (ordered sequence)
# ---------------------------------------------------------------------------


class TestJobRegisteredAtRefreshStart:
    def test_register_then_running_sequence_is_ordered(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_job_tracker,
    ):
        """register_job must be called BEFORE update_status(status='running').

        Verified via mock_calls ordering so a reversed sequence is caught.
        """
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=mock_job_tracker,
        )

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            scheduler._execute_refresh("my-repo-global")

        # Extract the names of all calls made on mock_job_tracker in order.
        call_names = [c[0] for c in mock_job_tracker.mock_calls]

        # register_job must appear before update_status.
        assert "register_job" in call_names, "register_job was never called"
        assert "update_status" in call_names, "update_status was never called"
        register_idx = call_names.index("register_job")
        update_idx = call_names.index("update_status")
        assert register_idx < update_idx, (
            f"register_job (pos {register_idx}) must precede "
            f"update_status (pos {update_idx})"
        )

        # The same job_id must flow through both calls.
        registered_job_id = mock_job_tracker.register_job.call_args[0][0]
        assert registered_job_id == "refresh-my-repo-global"

        update_args = mock_job_tracker.update_status.call_args
        assert update_args[0][0] == "refresh-my-repo-global"
        assert update_args[1].get("status") == "running"


# ---------------------------------------------------------------------------
# AC3: Job unregistered at refresh end (success)
# ---------------------------------------------------------------------------


class TestJobUnregisteredOnSuccess:
    def test_complete_job_called_on_success(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_job_tracker,
    ):
        """complete_job must be called when _execute_refresh exits successfully."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=mock_job_tracker,
        )

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            result = scheduler._execute_refresh("ok-repo-global")

        assert result["success"] is True
        mock_job_tracker.complete_job.assert_called_once_with("refresh-ok-repo-global")
        mock_job_tracker.fail_job.assert_not_called()


# ---------------------------------------------------------------------------
# AC4: Job unregistered on failure (try/finally guarantee)
# ---------------------------------------------------------------------------


class TestJobUnregisteredOnFailure:
    def test_fail_job_called_when_refresh_raises(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        mock_job_tracker,
    ):
        """fail_job must be called even when _execute_refresh raises an exception."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=mock_job_tracker,
        )

        with patch.object(
            scheduler.alias_manager,
            "read_alias",
            side_effect=RuntimeError("disk read error"),
        ):
            with pytest.raises(RuntimeError, match="disk read error"):
                scheduler._execute_refresh("failing-repo-global")

        mock_job_tracker.fail_job.assert_called_once()
        job_id_arg = mock_job_tracker.fail_job.call_args[0][0]
        assert job_id_arg == "refresh-failing-repo-global"
        mock_job_tracker.complete_job.assert_not_called()


# ---------------------------------------------------------------------------
# AC5: CLI mode — no job_tracker means no registration calls
# ---------------------------------------------------------------------------


class TestNoJobTrackerInCliMode:
    def test_no_job_tracker_no_registration(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """When job_tracker is None, _execute_refresh must run without touching any tracker."""
        scheduler = _make_scheduler(
            golden_repos_dir,
            config_mgr,
            query_tracker,
            cleanup_manager,
            job_tracker=None,
        )

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            result = scheduler._execute_refresh("cli-repo-global")

        assert result["success"] is True  # normal early return


# ---------------------------------------------------------------------------
# AC6: JobTracker exposes drain interface methods
# ---------------------------------------------------------------------------


class TestJobTrackerDrainInterface:
    """JobTracker must expose get_running_jobs_count() and get_queued_jobs_count()
    so it can be registered with MaintenanceState as a drain tracker."""

    def test_get_running_jobs_count_method_exists(self):
        from code_indexer.server.services.job_tracker import JobTracker

        assert hasattr(JobTracker, "get_running_jobs_count"), (
            "JobTracker must have get_running_jobs_count() for MaintenanceState interface"
        )

    def test_get_queued_jobs_count_method_exists(self):
        from code_indexer.server.services.job_tracker import JobTracker

        assert hasattr(JobTracker, "get_queued_jobs_count"), (
            "JobTracker must have get_queued_jobs_count() for MaintenanceState interface"
        )

    def test_get_running_jobs_count_returns_zero_when_empty(self, job_tracker_db):
        """get_running_jobs_count() returns 0 when no jobs are registered."""
        assert job_tracker_db.get_running_jobs_count() == 0

    def test_get_queued_jobs_count_returns_zero_when_empty(self, job_tracker_db):
        """get_queued_jobs_count() returns 0 when no jobs are pending."""
        assert job_tracker_db.get_queued_jobs_count() == 0

    def test_get_running_and_queued_counts_reflect_in_memory_jobs(self, job_tracker_db):
        """Both methods reflect the in-memory _active_jobs dict correctly."""
        from code_indexer.server.services.job_tracker import TrackedJob

        running_job = TrackedJob(
            job_id="job-run-1",
            operation_type="global_repo_refresh",
            status="running",
            username="system",
        )
        pending_job = TrackedJob(
            job_id="job-pend-1",
            operation_type="global_repo_refresh",
            status="pending",
            username="system",
        )

        with job_tracker_db._lock:
            job_tracker_db._active_jobs["job-run-1"] = running_job
            job_tracker_db._active_jobs["job-pend-1"] = pending_job

        assert job_tracker_db.get_running_jobs_count() == 1
        assert job_tracker_db.get_queued_jobs_count() == 1
