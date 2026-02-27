"""
AC3, AC4, AC5, AC6, AC7, AC8, AC10: ClaudeCliManager + JobTracker integration tests.

Story 3 (#313) - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC3: ClaudeCliManager accepts Optional[JobTracker] parameter
- AC4: ClaudeCliManager.process_all_fallbacks() registers as `catchup_processing` operation type
- AC5: ClaudeCliManager supports skip_tracking=True parameter to prevent double-tracking
- AC6: Progress updates during batch catch-up (e.g., "Processing repo 3/15")
- AC7: Job status transitions correctly for catchup_processing operations
- AC8: Failed operations report error details via fail_job()
- AC10: Unit tests cover both operation types including skip_tracking behavior
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.claude_cli_manager import ClaudeCliManager


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


def make_manager(job_tracker=None):
    """Helper to create ClaudeCliManager with given tracker (max_workers=0 to skip thread start)."""
    manager = ClaudeCliManager(
        api_key=None,
        max_workers=0,
        job_tracker=job_tracker,
    )
    return manager


# ---------------------------------------------------------------------------
# AC3: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestClaudeCliManagerConstructor:
    """AC3: ClaudeCliManager accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self):
        """
        ClaudeCliManager can be constructed without a job_tracker.

        Given no job_tracker is provided
        When ClaudeCliManager is instantiated
        Then no exception is raised
        """
        manager = ClaudeCliManager(api_key=None, max_workers=0)
        assert manager is not None

    def test_accepts_job_tracker_instance(self, job_tracker):
        """
        ClaudeCliManager stores the job_tracker when provided.

        Given a real JobTracker instance
        When ClaudeCliManager is instantiated with job_tracker=tracker
        Then _job_tracker attribute is set to the provided instance
        """
        manager = make_manager(job_tracker=job_tracker)
        assert manager._job_tracker is job_tracker

    def test_job_tracker_defaults_to_none(self):
        """
        _job_tracker is None when not provided.

        Given no job_tracker keyword argument
        When ClaudeCliManager is instantiated
        Then _job_tracker is None
        """
        manager = ClaudeCliManager(api_key=None, max_workers=0)
        assert manager._job_tracker is None

    def test_existing_parameters_still_work(self, job_tracker):
        """
        Existing constructor parameters continue to function alongside job_tracker.

        Given all constructor parameters are provided including job_tracker
        When ClaudeCliManager is instantiated
        Then all attributes are set correctly
        """
        manager = ClaudeCliManager(
            api_key="sk-test-key",
            max_workers=0,
            job_tracker=job_tracker,
        )
        assert manager._api_key == "sk-test-key"
        assert manager._max_workers == 0
        assert manager._job_tracker is job_tracker


# ---------------------------------------------------------------------------
# AC4: process_all_fallbacks registers as `catchup_processing`
# ---------------------------------------------------------------------------


class TestCatchupProcessingJobRegistration:
    """AC4: process_all_fallbacks() registers as catchup_processing operation type."""

    def test_process_all_fallbacks_registers_catchup_job(
        self, job_tracker, tmp_path
    ):
        """
        When process_all_fallbacks() is called,
        Then a job with operation_type='catchup_processing' is registered.

        Given a ClaudeCliManager with a real JobTracker and meta_dir with fallbacks
        When process_all_fallbacks() is called (with CLI check mocked)
        Then the job_tracker contains a catchup_processing job
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        # Create a fallback file
        fallback = meta_dir / "my-repo_README.md"
        fallback.write_text("# Fallback README")

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

        with patch.object(manager, "check_cli_available", return_value=True), \
             patch.object(manager, "_process_single_fallback", return_value=True), \
             patch.object(manager, "_commit_and_reindex", return_value=None):
            manager.process_all_fallbacks()

        catchup_jobs = [
            j for j in registered_jobs if j["operation_type"] == "catchup_processing"
        ]
        assert len(catchup_jobs) == 1
        assert catchup_jobs[0]["username"] == "system"
        # catchup_processing is a global operation, no repo_alias
        assert catchup_jobs[0]["repo_alias"] is None

    def test_process_all_fallbacks_no_fallbacks_still_registers_job(
        self, job_tracker, tmp_path
    ):
        """
        Even with no fallbacks, process_all_fallbacks still registers a catchup job
        and completes it immediately.

        Given a ClaudeCliManager with a real JobTracker and an empty meta_dir
        When process_all_fallbacks() is called
        Then a catchup_processing job is registered and completed
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        registered_ids = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            if operation_type == "catchup_processing":
                registered_ids.append(job_id)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True):
            manager.process_all_fallbacks()

        assert len(registered_ids) == 1
        job = job_tracker.get_job(registered_ids[0])
        assert job is not None
        assert job.status == "completed"


# ---------------------------------------------------------------------------
# AC5: skip_tracking=True prevents double-tracking
# ---------------------------------------------------------------------------


class TestSkipTracking:
    """AC5: ClaudeCliManager supports skip_tracking=True to prevent double-tracking."""

    def test_skip_tracking_true_prevents_job_registration(
        self, job_tracker, tmp_path
    ):
        """
        When process_all_fallbacks(skip_tracking=True) is called,
        Then no job is registered in the tracker.

        Given a ClaudeCliManager with a real JobTracker
        And skip_tracking=True is passed
        When process_all_fallbacks is invoked
        Then register_job is never called on the tracker
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        fallback = meta_dir / "repo-x_README.md"
        fallback.write_text("# Fallback")

        registered_operations = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            registered_operations.append(operation_type)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True), \
             patch.object(manager, "_process_single_fallback", return_value=True), \
             patch.object(manager, "_commit_and_reindex", return_value=None):
            manager.process_all_fallbacks(skip_tracking=True)

        # No catchup_processing job should be registered
        catchup_jobs = [op for op in registered_operations if op == "catchup_processing"]
        assert len(catchup_jobs) == 0

    def test_skip_tracking_false_still_registers_job(
        self, job_tracker, tmp_path
    ):
        """
        When process_all_fallbacks(skip_tracking=False) is called,
        Then a job IS registered (same as default behavior).

        Given a ClaudeCliManager with a real JobTracker
        And skip_tracking=False is passed
        When process_all_fallbacks is invoked
        Then a catchup_processing job is registered
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        registered_operations = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            registered_operations.append(operation_type)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True):
            manager.process_all_fallbacks(skip_tracking=False)

        catchup_jobs = [op for op in registered_operations if op == "catchup_processing"]
        assert len(catchup_jobs) == 1

    def test_skip_tracking_defaults_to_false(
        self, job_tracker, tmp_path
    ):
        """
        When process_all_fallbacks() is called without skip_tracking arg,
        Then a catchup job IS registered (default behavior is tracking enabled).

        Given a ClaudeCliManager with a real JobTracker
        When process_all_fallbacks() is called without explicit skip_tracking
        Then a catchup_processing job is registered
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        registered_operations = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            registered_operations.append(operation_type)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True):
            manager.process_all_fallbacks()  # no skip_tracking arg

        catchup_jobs = [op for op in registered_operations if op == "catchup_processing"]
        assert len(catchup_jobs) == 1

    def test_skip_tracking_true_with_no_tracker_still_works(self, tmp_path):
        """
        When skip_tracking=True and no tracker is configured, no exception occurs.

        Given a ClaudeCliManager with NO job_tracker
        When process_all_fallbacks(skip_tracking=True) is called
        Then no exception is raised
        """
        manager = make_manager(job_tracker=None)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        with patch.object(manager, "check_cli_available", return_value=True):
            result = manager.process_all_fallbacks(skip_tracking=True)

        assert result is not None  # CatchupResult returned


# ---------------------------------------------------------------------------
# AC6: Progress updates during batch catch-up
# ---------------------------------------------------------------------------


class TestCatchupProgressUpdates:
    """AC6: Progress updates during batch catch-up (e.g., 'Processing repo 3/15')."""

    def test_progress_info_updated_during_processing(
        self, job_tracker, tmp_path
    ):
        """
        During process_all_fallbacks, progress_info is updated with 'Processing repo X/Y'.

        Given a ClaudeCliManager with a real JobTracker and multiple fallbacks
        When process_all_fallbacks() is called
        Then progress_info is updated at least once with 'Processing repo' text
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        # Create 3 fallback files
        for i in range(1, 4):
            (meta_dir / f"repo-{i}_README.md").write_text(f"# Fallback {i}")

        progress_updates = []
        original_update = job_tracker.update_status

        def capturing_update(job_id, status=None, progress=None, progress_info=None, **kwargs):
            if progress_info and "Processing repo" in progress_info:
                progress_updates.append(progress_info)
            return original_update(
                job_id, status=status, progress=progress, progress_info=progress_info, **kwargs
            )

        job_tracker.update_status = capturing_update

        with patch.object(manager, "check_cli_available", return_value=True), \
             patch.object(manager, "_process_single_fallback", return_value=True), \
             patch.object(manager, "_commit_and_reindex", return_value=None):
            manager.process_all_fallbacks()

        assert len(progress_updates) >= 1
        # Verify format includes repo count info
        assert any("3" in update for update in progress_updates)


# ---------------------------------------------------------------------------
# AC7: Job status transitions correctly for catchup_processing
# ---------------------------------------------------------------------------


class TestCatchupStatusTransitions:
    """AC7: Job status transitions correctly for catchup_processing operations."""

    def test_successful_catchup_transitions_to_completed(
        self, job_tracker, tmp_path
    ):
        """
        A catchup_processing job transitions to completed when all fallbacks succeed.

        Given a ClaudeCliManager with a real JobTracker and one fallback
        When process_all_fallbacks() is called and succeeds
        Then the job status is 'completed'
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        (meta_dir / "ok-repo_README.md").write_text("# Fallback")

        registered_id = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            if operation_type == "catchup_processing":
                registered_id.append(job_id)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True), \
             patch.object(manager, "_process_single_fallback", return_value=True), \
             patch.object(manager, "_commit_and_reindex", return_value=None):
            manager.process_all_fallbacks()

        assert len(registered_id) == 1
        job = job_tracker.get_job(registered_id[0])
        assert job is not None
        assert job.status == "completed"
        assert job.completed_at is not None

    def test_failed_catchup_transitions_to_failed(
        self, job_tracker, tmp_path
    ):
        """
        A catchup_processing job transitions to failed when processing fails.

        Given a ClaudeCliManager with a real JobTracker and one fallback
        When process_all_fallbacks() is called and processing fails
        Then the job status is 'failed'
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        (meta_dir / "bad-repo_README.md").write_text("# Fallback")

        registered_id = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            if operation_type == "catchup_processing":
                registered_id.append(job_id)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=True), \
             patch.object(manager, "_process_single_fallback", return_value=False):
            manager.process_all_fallbacks()

        assert len(registered_id) == 1
        job = job_tracker.get_job(registered_id[0])
        assert job is not None
        assert job.status == "failed"
        assert job.completed_at is not None

    def test_cli_unavailable_catchup_transitions_to_failed(
        self, job_tracker, tmp_path
    ):
        """
        When CLI is unavailable, the catchup job is registered and fails.

        Given a ClaudeCliManager with a real JobTracker
        When process_all_fallbacks() is called but CLI is unavailable
        Then the catchup_processing job is registered and transitions to 'failed'
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        (meta_dir / "any-repo_README.md").write_text("# Fallback")

        registered_id = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            if operation_type == "catchup_processing":
                registered_id.append(job_id)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=False):
            manager.process_all_fallbacks()

        assert len(registered_id) == 1
        job = job_tracker.get_job(registered_id[0])
        assert job is not None
        assert job.status == "failed"
        assert "CLI not available" in (job.error or "")


# ---------------------------------------------------------------------------
# AC8: Failed operations report error details
# ---------------------------------------------------------------------------


class TestCatchupErrorReporting:
    """AC8: Failed operations report error details via fail_job()."""

    def test_cli_unavailable_error_in_failed_job(
        self, job_tracker, tmp_path
    ):
        """
        When CLI is unavailable, error details are stored in the failed job.

        Given a ClaudeCliManager with a real JobTracker
        When process_all_fallbacks() is called and CLI is unavailable
        Then the job error contains meaningful information
        """
        manager = make_manager(job_tracker=job_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        (meta_dir / "x-repo_README.md").write_text("# Fallback")

        registered_id = []
        original_register = job_tracker.register_job

        def capturing_register(job_id, operation_type, username, **kwargs):
            if operation_type == "catchup_processing":
                registered_id.append(job_id)
            return original_register(job_id, operation_type, username, **kwargs)

        job_tracker.register_job = capturing_register

        with patch.object(manager, "check_cli_available", return_value=False):
            manager.process_all_fallbacks()

        assert len(registered_id) == 1
        job = job_tracker.get_job(registered_id[0])
        assert job.status == "failed"
        assert job.error is not None
        assert len(job.error) > 0

    def test_tracker_failure_does_not_break_process_all_fallbacks(
        self, tmp_path
    ):
        """
        If the job tracker raises exceptions, process_all_fallbacks still completes.

        Given a ClaudeCliManager with a broken job tracker
        When process_all_fallbacks() is called
        Then no exception propagates and a CatchupResult is returned
        """
        broken_tracker = MagicMock()
        broken_tracker.register_job.side_effect = RuntimeError("DB down")
        broken_tracker.update_status.side_effect = RuntimeError("DB down")
        broken_tracker.complete_job.side_effect = RuntimeError("DB down")
        broken_tracker.fail_job.side_effect = RuntimeError("DB down")

        manager = make_manager(job_tracker=broken_tracker)

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        with patch.object(manager, "check_cli_available", return_value=True):
            result = manager.process_all_fallbacks()

        # Must return a CatchupResult regardless of tracker failure
        assert result is not None
        from code_indexer.server.services.claude_cli_manager import CatchupResult
        assert isinstance(result, CatchupResult)


# ---------------------------------------------------------------------------
# AC10: Both operation types covered - skip_tracking integration with tracker=None
# ---------------------------------------------------------------------------


class TestSkipTrackingWithNoTracker:
    """
    AC10: When no tracker is configured, skip_tracking has no observable effect
    but must not raise exceptions.
    """

    def test_no_tracker_skip_tracking_false_runs_cleanly(self, tmp_path):
        """
        No tracker + skip_tracking=False: runs normally without error.
        """
        manager = make_manager(job_tracker=None)
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        with patch.object(manager, "check_cli_available", return_value=True):
            result = manager.process_all_fallbacks(skip_tracking=False)

        assert result is not None

    def test_no_tracker_skip_tracking_true_runs_cleanly(self, tmp_path):
        """
        No tracker + skip_tracking=True: runs normally without error.
        """
        manager = make_manager(job_tracker=None)
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        manager.set_meta_dir(meta_dir)

        with patch.object(manager, "check_cli_available", return_value=True):
            result = manager.process_all_fallbacks(skip_tracking=True)

        assert result is not None
