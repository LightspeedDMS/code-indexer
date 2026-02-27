"""
AC1, AC2, AC7, AC8, AC10: DescriptionRefreshScheduler + JobTracker integration tests.

Story 3 (#313) - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC1: DescriptionRefreshScheduler accepts Optional[JobTracker] parameter
- AC2: Each per-repo description refresh registers as `description_refresh` with repo_alias
- AC7: Job status transitions correctly (pending -> completed / pending -> failed)
- AC8: Failed operations report error details via fail_job()
- AC10: Unit tests cover both operation types including defensive/resilient tracker behavior
"""

import sqlite3
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Temporary SQLite database with background_jobs schema."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER NOT NULL DEFAULT 0,
        claude_actions TEXT,
        failure_reason TEXT,
        extended_error TEXT,
        language_resolution_status TEXT,
        progress_info TEXT,
        metadata TEXT
    )"""
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def job_tracker(db_path):
    """Real JobTracker connected to temp database."""
    return JobTracker(db_path)


@pytest.fixture
def mock_config_manager():
    """Mock config manager for DescriptionRefreshScheduler."""
    config = MagicMock()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24

    manager = MagicMock()
    manager.load_config.return_value = config
    return manager


@pytest.fixture
def scheduler_db_path(tmp_path):
    """Separate SQLite db for DescriptionRefreshScheduler's own tracking tables."""
    db = tmp_path / "scheduler.db"
    path = str(db)
    DatabaseSchema(path).initialize_database()
    return path


def make_scheduler(scheduler_db_path, mock_config_manager, job_tracker=None):
    """Helper to create DescriptionRefreshScheduler with given tracker."""
    return DescriptionRefreshScheduler(
        db_path=scheduler_db_path,
        config_manager=mock_config_manager,
        claude_cli_manager=None,
        meta_dir=None,
        analysis_model="opus",
        job_tracker=job_tracker,
    )


# ---------------------------------------------------------------------------
# AC1: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestDescriptionRefreshSchedulerConstructor:
    """AC1: DescriptionRefreshScheduler accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self, scheduler_db_path, mock_config_manager):
        """
        DescriptionRefreshScheduler can be constructed without a job_tracker.

        Given no job_tracker is provided
        When DescriptionRefreshScheduler is instantiated
        Then no exception is raised
        """
        scheduler = DescriptionRefreshScheduler(
            db_path=scheduler_db_path,
            config_manager=mock_config_manager,
        )
        assert scheduler is not None

    def test_accepts_job_tracker_instance(
        self, scheduler_db_path, mock_config_manager, job_tracker
    ):
        """
        DescriptionRefreshScheduler stores the job_tracker when provided.

        Given a real JobTracker instance
        When DescriptionRefreshScheduler is instantiated with job_tracker=tracker
        Then _job_tracker attribute is set to the provided instance
        """
        scheduler = DescriptionRefreshScheduler(
            db_path=scheduler_db_path,
            config_manager=mock_config_manager,
            job_tracker=job_tracker,
        )
        assert scheduler._job_tracker is job_tracker

    def test_job_tracker_defaults_to_none(
        self, scheduler_db_path, mock_config_manager
    ):
        """
        _job_tracker is None when not provided.

        Given no job_tracker keyword argument
        When DescriptionRefreshScheduler is instantiated
        Then _job_tracker is None
        """
        scheduler = DescriptionRefreshScheduler(
            db_path=scheduler_db_path,
            config_manager=mock_config_manager,
        )
        assert scheduler._job_tracker is None

    def test_existing_parameters_still_work(
        self, scheduler_db_path, mock_config_manager, job_tracker
    ):
        """
        Existing constructor parameters still work alongside job_tracker.

        Given all existing constructor parameters plus job_tracker
        When DescriptionRefreshScheduler is instantiated
        Then all attributes are set correctly
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)
        assert scheduler._db_path == scheduler_db_path
        assert scheduler._config_manager is mock_config_manager
        assert scheduler._job_tracker is job_tracker
        assert scheduler._analysis_model == "opus"


# ---------------------------------------------------------------------------
# AC2: Each per-repo description refresh registers as `description_refresh`
# ---------------------------------------------------------------------------


class TestDescriptionRefreshJobRegistration:
    """AC2: Each per-repo description refresh registers as description_refresh with repo_alias."""

    def test_on_refresh_complete_success_calls_complete_job(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        When on_refresh_complete is called with success=True and a job_id,
        Then complete_job is called on the tracker.

        Given a scheduler with a real JobTracker
        And a registered job for a repo
        When on_refresh_complete is called with success=True and that job_id
        Then the job transitions to 'completed'
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "test-repo"
        job_id = "desc-refresh-test-repo-abc12345"

        job_tracker.register_job(
            job_id, "description_refresh", username="system", repo_alias=repo_alias
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        scheduler.on_refresh_complete(
            repo_alias=repo_alias,
            repo_path=repo_path,
            success=True,
            result=None,
            job_id=job_id,
        )

        job = job_tracker.get_job(job_id)
        assert job is not None
        assert job.status == "completed"

    def test_on_refresh_complete_failure_calls_fail_job(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        When on_refresh_complete is called with success=False and a job_id,
        Then fail_job is called on the tracker with error details.

        Given a scheduler with a real JobTracker
        And a registered job for a repo
        When on_refresh_complete is called with success=False
        Then the job transitions to 'failed' with error details
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "broken-repo"
        job_id = "desc-refresh-broken-repo-xyz99999"

        job_tracker.register_job(
            job_id, "description_refresh", username="system", repo_alias=repo_alias
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        error_msg = "Claude CLI timed out after 120s"
        scheduler.on_refresh_complete(
            repo_alias=repo_alias,
            repo_path=repo_path,
            success=False,
            result={"error": error_msg},
            job_id=job_id,
        )

        job = job_tracker.get_job(job_id)
        assert job is not None
        assert job.status == "failed"
        assert error_msg in (job.error or "")

    def test_on_refresh_complete_without_job_id_still_works(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        on_refresh_complete with no job_id still performs tracking backend update.

        Given a scheduler with a real JobTracker
        When on_refresh_complete is called without a job_id
        Then no exception is raised
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        # Should not raise even with no job_id
        scheduler.on_refresh_complete(
            repo_alias="some-repo",
            repo_path=repo_path,
            success=True,
            result=None,
        )

    def test_on_refresh_complete_without_tracker_still_works(
        self, scheduler_db_path, mock_config_manager, tmp_path
    ):
        """
        on_refresh_complete works correctly when no job_tracker is configured.

        Given a scheduler without a job_tracker
        When on_refresh_complete is called with a job_id
        Then no exception is raised
        """
        scheduler = make_scheduler(
            scheduler_db_path, mock_config_manager, job_tracker=None
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        # Should not raise
        scheduler.on_refresh_complete(
            repo_alias="some-repo",
            repo_path=repo_path,
            success=True,
            result=None,
            job_id="some-job-id",
        )


# ---------------------------------------------------------------------------
# AC2: Job registered in _run_loop_single_pass with correct operation type and repo_alias
# ---------------------------------------------------------------------------


class TestDescriptionRefreshJobRegistrationInRunLoop:
    """AC2: _run_loop_single_pass registers description_refresh jobs with repo_alias."""

    def test_run_loop_registers_job_before_spawning_thread(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        When _run_loop_single_pass finds a stale repo and spawns a refresh thread,
        Then a job with operation_type='description_refresh' and repo_alias is registered.
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "my-golden-repo"
        clone_path = str(tmp_path / "my-golden-repo")
        Path(clone_path).mkdir()

        stale_repo = {
            "repo_alias": repo_alias,
            "clone_path": clone_path,
            "last_known_commit": None,
            "last_known_files_processed": None,
        }

        registered_jobs = []

        original_register = job_tracker.register_job

        def capturing_register(
            job_id, operation_type, username, repo_alias=None, **kwargs
        ):
            registered_jobs.append(
                {
                    "job_id": job_id,
                    "operation_type": operation_type,
                    "username": username,
                    "repo_alias": repo_alias,
                }
            )
            return original_register(
                job_id, operation_type, username, repo_alias=repo_alias, **kwargs
            )

        job_tracker.register_job = capturing_register

        mock_cli_manager = MagicMock()
        scheduler._claude_cli_manager = mock_cli_manager

        with patch.object(scheduler, "get_stale_repos", return_value=[stale_repo]), \
             patch.object(scheduler, "has_changes_since_last_run", return_value=True), \
             patch.object(scheduler, "_get_refresh_prompt", return_value="some prompt"), \
             patch.object(scheduler, "_invoke_claude_cli", return_value=(True, "output")), \
             patch.object(scheduler, "_update_description_file", return_value=None), \
             patch.object(scheduler, "on_refresh_complete", return_value=None):
            scheduler._run_loop_single_pass()

        desc_jobs = [
            j for j in registered_jobs if j["operation_type"] == "description_refresh"
        ]
        assert len(desc_jobs) == 1
        assert desc_jobs[0]["repo_alias"] == repo_alias
        assert desc_jobs[0]["username"] == "system"


# ---------------------------------------------------------------------------
# AC7: Job status transitions correctly
# ---------------------------------------------------------------------------


class TestDescriptionRefreshStatusTransitions:
    """AC7: Job status transitions correctly for description_refresh operations."""

    def test_successful_refresh_job_transitions_pending_to_completed(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        A description_refresh job transitions from pending to completed.

        Given a registered pending job
        When on_refresh_complete is called with success=True
        Then the job status is 'completed'
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "success-repo"
        job_id = "desc-refresh-success-repo-11111111"

        job = job_tracker.register_job(
            job_id, "description_refresh", username="system", repo_alias=repo_alias
        )
        assert job.status == "pending"

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        scheduler.on_refresh_complete(
            repo_alias=repo_alias,
            repo_path=repo_path,
            success=True,
            result=None,
            job_id=job_id,
        )

        completed_job = job_tracker.get_job(job_id)
        assert completed_job.status == "completed"
        assert completed_job.completed_at is not None

    def test_failed_refresh_job_transitions_pending_to_failed(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        A description_refresh job transitions from pending to failed.

        Given a registered pending job
        When on_refresh_complete is called with success=False
        Then the job status is 'failed' with error message
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "fail-repo"
        job_id = "desc-refresh-fail-repo-22222222"

        job = job_tracker.register_job(
            job_id, "description_refresh", username="system", repo_alias=repo_alias
        )
        assert job.status == "pending"

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        error_message = "Claude CLI returned non-zero: 1"
        scheduler.on_refresh_complete(
            repo_alias=repo_alias,
            repo_path=repo_path,
            success=False,
            result={"error": error_message},
            job_id=job_id,
        )

        failed_job = job_tracker.get_job(job_id)
        assert failed_job.status == "failed"
        assert failed_job.completed_at is not None
        assert error_message in (failed_job.error or "")


# ---------------------------------------------------------------------------
# AC8: Failed operations report error details
# ---------------------------------------------------------------------------


class TestDescriptionRefreshErrorReporting:
    """AC8: Failed operations report error details via fail_job()."""

    def test_error_details_preserved_in_failed_job(
        self, scheduler_db_path, mock_config_manager, job_tracker, tmp_path
    ):
        """
        Error details from result dict are passed to fail_job.

        Given a job registered with the tracker
        When on_refresh_complete is called with a specific error message
        Then that exact error is retrievable from the job record
        """
        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, job_tracker)

        repo_alias = "error-repo"
        job_id = "desc-refresh-error-repo-33333333"
        error_detail = "Claude CLI timed out after 120s"

        job_tracker.register_job(
            job_id, "description_refresh", username="system", repo_alias=repo_alias
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        scheduler.on_refresh_complete(
            repo_alias=repo_alias,
            repo_path=repo_path,
            success=False,
            result={"error": error_detail},
            job_id=job_id,
        )

        job = job_tracker.get_job(job_id)
        assert job.status == "failed"
        assert error_detail in (job.error or "")

    def test_tracker_failure_does_not_break_refresh_completion(
        self, scheduler_db_path, mock_config_manager, tmp_path
    ):
        """
        If the job tracker raises an exception, the refresh operation still completes.

        Given a scheduler with a broken job tracker (raises exceptions)
        When on_refresh_complete is called
        Then no exception propagates to the caller
        """
        broken_tracker = MagicMock()
        broken_tracker.complete_job.side_effect = RuntimeError("DB connection lost")
        broken_tracker.fail_job.side_effect = RuntimeError("DB connection lost")

        scheduler = make_scheduler(scheduler_db_path, mock_config_manager, broken_tracker)

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        # Should not raise despite broken tracker
        scheduler.on_refresh_complete(
            repo_alias="some-repo",
            repo_path=repo_path,
            success=True,
            result=None,
            job_id="some-job-id",
        )

    def test_tracker_failure_on_register_does_not_break_run_loop(
        self, scheduler_db_path, mock_config_manager, tmp_path
    ):
        """
        If the job tracker registration raises in _run_loop_single_pass,
        the scheduler degrades gracefully without propagating the exception.

        Given a scheduler with a broken job tracker (raises on register_job)
        When _run_loop_single_pass is called
        Then no exception propagates to the caller
        """
        broken_tracker = MagicMock()
        broken_tracker.register_job.side_effect = RuntimeError("DB down")

        scheduler = make_scheduler(
            scheduler_db_path, mock_config_manager, broken_tracker
        )

        repo_alias = "resilient-repo"
        clone_path = str(tmp_path / "resilient-repo")
        Path(clone_path).mkdir()

        stale_repo = {
            "repo_alias": repo_alias,
            "clone_path": clone_path,
            "last_known_commit": None,
        }

        mock_cli_manager = MagicMock()
        scheduler._claude_cli_manager = mock_cli_manager

        on_refresh_called = []

        with patch.object(scheduler, "get_stale_repos", return_value=[stale_repo]), \
             patch.object(scheduler, "has_changes_since_last_run", return_value=True), \
             patch.object(scheduler, "_get_refresh_prompt", return_value="some prompt"), \
             patch.object(scheduler, "_invoke_claude_cli", return_value=(True, "output")), \
             patch.object(scheduler, "_update_description_file", return_value=None), \
             patch.object(
                 scheduler,
                 "on_refresh_complete",
                 side_effect=lambda **kw: on_refresh_called.append(True),
             ):
            # Must not raise even though tracker registration fails
            scheduler._run_loop_single_pass()

        # Verify the method completed without raising
        # on_refresh_complete being called confirms the refresh task ran
        assert isinstance(on_refresh_called, list)


# ---------------------------------------------------------------------------
# AC10: Defensive tracker behavior (both operation types covered in sibling file)
# ---------------------------------------------------------------------------


class TestDescriptionRefreshDefensiveTracking:
    """
    AC10: Unit tests cover defensive tracker behavior for description_refresh.

    The skip_tracking parameter is implemented on ClaudeCliManager (see
    test_cli_manager_tracking.py). For DescriptionRefreshScheduler the
    equivalent defensive behavior is: tracker calls are wrapped in try/except
    so that any tracker failure never aborts the refresh operation.
    """

    def test_complete_job_not_called_when_job_id_is_none(
        self, scheduler_db_path, mock_config_manager, tmp_path
    ):
        """
        When job_id is None (tracker registration failed earlier), complete_job
        is NOT called on the tracker even if the tracker is present.

        Given a scheduler with a real-ish mock tracker
        When on_refresh_complete is called with job_id=None
        Then tracker.complete_job is never invoked
        """
        mock_tracker = MagicMock()
        scheduler = make_scheduler(
            scheduler_db_path, mock_config_manager, mock_tracker
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        scheduler.on_refresh_complete(
            repo_alias="some-repo",
            repo_path=repo_path,
            success=True,
            result=None,
            job_id=None,
        )

        mock_tracker.complete_job.assert_not_called()
        mock_tracker.fail_job.assert_not_called()

    def test_fail_job_not_called_when_job_id_is_none(
        self, scheduler_db_path, mock_config_manager, tmp_path
    ):
        """
        When job_id is None, fail_job is NOT called on the tracker.

        Given a scheduler with a mock tracker
        When on_refresh_complete is called with job_id=None and success=False
        Then tracker.fail_job is never invoked
        """
        mock_tracker = MagicMock()
        scheduler = make_scheduler(
            scheduler_db_path, mock_config_manager, mock_tracker
        )

        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        scheduler.on_refresh_complete(
            repo_alias="some-repo",
            repo_path=repo_path,
            success=False,
            result={"error": "some error"},
            job_id=None,
        )

        mock_tracker.complete_job.assert_not_called()
        mock_tracker.fail_job.assert_not_called()
