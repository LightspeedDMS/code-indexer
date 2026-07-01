"""
Unit tests for JobTracker.complete_job and JobTracker.fail_job.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC1: complete_job, fail_job

Bug #1258: complete_job/fail_job absent-from-memory hardening.  Adds a
persisted-row fallback so a redundant terminal call (e.g. the benign
description-refresh double-dispatch where LifecycleBatchRunner.run() already
completed the job before the scheduler's own on_refresh_complete calls
complete_job/fail_job again) is a silent, idempotent no-op logged at DEBUG,
while a genuine "in-memory lost but DB row still non-terminal" zombie edge
(pop-before-persist DB write failure) is force-corrected to a terminal status
and logged at WARNING.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone


class TestJobTrackerComplete:
    """Tests for complete_job (AC1)."""

    def test_complete_job_sets_completed_status(self, tracker):
        """
        complete_job sets the job status to 'completed' in SQLite.

        Given a running job
        When complete_job is called
        Then the job status is 'completed'
        """
        tracker.register_job("job-comp-001", "dep_map_analysis", "admin")
        tracker.update_status("job-comp-001", status="running")
        tracker.complete_job("job-comp-001")

        job = tracker.get_job("job-comp-001")
        assert job is not None
        assert job.status == "completed"

    def test_complete_job_sets_completed_at(self, tracker):
        """
        complete_job sets completed_at to a recent UTC datetime.

        Given a running job
        When complete_job is called
        Then completed_at is set to a recent timestamp
        """
        before = datetime.now(timezone.utc)
        tracker.register_job("job-compAt-001", "dep_map_analysis", "admin")
        tracker.update_status("job-compAt-001", status="running")
        tracker.complete_job("job-compAt-001")
        after = datetime.now(timezone.utc)

        job = tracker.get_job("job-compAt-001")
        assert job is not None
        assert job.completed_at is not None
        assert before <= job.completed_at <= after

    def test_complete_job_removes_from_memory(self, tracker):
        """
        complete_job removes the job from the in-memory active jobs dict.

        Given a running job
        When complete_job is called
        Then the job no longer appears in get_active_jobs()
        """
        tracker.register_job("job-remMem-001", "dep_map_analysis", "admin")
        tracker.update_status("job-remMem-001", status="running")
        tracker.complete_job("job-remMem-001")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-remMem-001" not in active_ids

    def test_complete_job_persists_to_sqlite(self, tracker, db_path):
        """
        complete_job persists the completed state to SQLite.

        Given a running job that is completed
        When querying SQLite directly
        Then the row shows status='completed'
        """
        tracker.register_job("job-sqlComp-001", "dep_map_analysis", "admin")
        tracker.update_status("job-sqlComp-001", status="running")
        tracker.complete_job("job-sqlComp-001")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?",
            ("job-sqlComp-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"

    def test_complete_job_with_result(self, tracker, db_path):
        """
        complete_job stores the result dict as JSON in SQLite.

        Given a result dict is provided
        When complete_job is called
        Then the result is stored as JSON in SQLite
        """
        result = {"total_files": 42, "duration_ms": 1234}
        tracker.register_job("job-result-001", "dep_map_analysis", "admin")
        tracker.update_status("job-result-001", status="running")
        tracker.complete_job("job-result-001", result=result)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT result FROM background_jobs WHERE job_id = ?",
            ("job-result-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        stored_result = json.loads(row[0])
        assert stored_result == result

    def test_complete_job_ignores_missing(self, tracker):
        """
        complete_job silently ignores unknown job_id.

        Given a job_id that does not exist in memory
        When complete_job is called
        Then no exception is raised
        """
        tracker.complete_job("nonexistent-job")


class TestJobTrackerFail:
    """Tests for fail_job (AC1)."""

    def test_fail_job_sets_failed_status(self, tracker):
        """
        fail_job sets job status to 'failed'.

        Given a running job
        When fail_job is called
        Then the job status is 'failed'
        """
        tracker.register_job("job-fail-001", "dep_map_analysis", "admin")
        tracker.update_status("job-fail-001", status="running")
        tracker.fail_job("job-fail-001", error="Something went wrong")

        job = tracker.get_job("job-fail-001")
        assert job is not None
        assert job.status == "failed"

    def test_fail_job_sets_error_message(self, tracker):
        """
        fail_job stores the error message on the job.

        Given a running job
        When fail_job(error='Timeout') is called
        Then the job's error field is 'Timeout'
        """
        tracker.register_job("job-errMsg-001", "dep_map_analysis", "admin")
        tracker.update_status("job-errMsg-001", status="running")
        tracker.fail_job("job-errMsg-001", error="Timeout")

        job = tracker.get_job("job-errMsg-001")
        assert job is not None
        assert job.error == "Timeout"

    def test_fail_job_removes_from_memory(self, tracker):
        """
        fail_job removes the job from the in-memory active jobs dict.

        Given a running job
        When fail_job is called
        Then the job no longer appears in get_active_jobs()
        """
        tracker.register_job("job-failMem-001", "dep_map_analysis", "admin")
        tracker.update_status("job-failMem-001", status="running")
        tracker.fail_job("job-failMem-001", error="Error")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-failMem-001" not in active_ids

    def test_fail_job_persists_to_sqlite(self, tracker, db_path):
        """
        fail_job persists the failed state to SQLite.

        Given a job that has been failed
        When querying SQLite directly
        Then the row shows status='failed' and the error message
        """
        tracker.register_job("job-sqlFail-001", "dep_map_analysis", "admin")
        tracker.update_status("job-sqlFail-001", status="running")
        tracker.fail_job("job-sqlFail-001", error="DB connection lost")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs WHERE job_id = ?",
            ("job-sqlFail-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "DB connection lost"

    def test_fail_job_ignores_missing(self, tracker):
        """
        fail_job silently ignores unknown job_id.

        Given a job_id that does not exist in memory
        When fail_job is called
        Then no exception is raised
        """
        tracker.fail_job("nonexistent-job", error="irrelevant")


class TestCompleteJobAbsentFallback1258:
    """Bug #1258: complete_job absent-from-memory persisted-row fallback."""

    def test_complete_job_already_completed_in_db_logs_debug_not_warning(
        self, tracker, caplog
    ):
        """
        Benign double-dispatch: DB row is ALREADY 'completed' (e.g. the real
        LifecycleBatchRunner.run() already called complete_job on this job_id)
        when a second complete_job call arrives.  Must log DEBUG, not WARNING,
        and must not raise.
        """
        tracker.register_job("job-dbl-comp-001", "description_refresh", "system")
        tracker.update_status("job-dbl-comp-001", status="running")
        tracker.complete_job("job-dbl-comp-001", result={"phase": "first"})

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.complete_job("job-dbl-comp-001", result={"phase": "second"})

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [], (
            f"Expected no WARNING, got: {[r.message for r in warnings]}"
        )

    def test_complete_job_already_completed_in_db_does_not_overwrite_result(
        self, tracker, db_path
    ):
        """First-terminal-write wins: the redundant complete_job must not
        clobber the original result/status already persisted."""
        tracker.register_job("job-dbl-comp-002", "description_refresh", "system")
        tracker.update_status("job-dbl-comp-002", status="running")
        tracker.complete_job("job-dbl-comp-002", result={"phase": "first"})

        # Redundant second call (simulates on_refresh_complete's own call
        # arriving after LifecycleBatchRunner.run() already finalized it).
        tracker.complete_job("job-dbl-comp-002", result={"phase": "second"})

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, result FROM background_jobs WHERE job_id = ?",
            ("job-dbl-comp-002",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"
        assert json.loads(row[1]) == {"phase": "first"}

    def test_complete_job_absent_and_db_row_still_running_forces_terminal_update(
        self, tracker, db_path, caplog
    ):
        """
        Genuine zombie edge: the in-memory TrackedJob was lost (e.g. a crash
        between the pop and the persist in a prior complete_job call) while
        the DB row is still 'running'.  A subsequent complete_job call for the
        same job_id must force the DB row to 'completed' rather than silently
        no-op, and must log at WARNING (this path indicates a real gap).
        """
        tracker.register_job("job-zombie-comp-001", "description_refresh", "system")
        tracker.update_status("job-zombie-comp-001", status="running")

        # Simulate the in-memory object being lost while DB still says "running"
        # (Bug #1258's pop-before-persist edge: _upsert_job raised after the pop).
        with tracker._lock:
            del tracker._active_jobs["job-zombie-comp-001"]

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.complete_job("job-zombie-comp-001", result={"forced": True})

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, result, completed_at FROM background_jobs WHERE job_id = ?",
            ("job-zombie-comp-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"
        assert json.loads(row[1]) == {"forced": True}
        assert row[2] is not None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got: {[r.message for r in warnings]}"
        )

    def test_complete_job_absent_and_no_db_row_logs_warning_and_noops(
        self, tracker, caplog
    ):
        """No DB row at all: logs WARNING (not silently swallowed) and does
        not raise."""
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.complete_job("nonexistent-job-with-warning-check")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got: {[r.message for r in warnings]}"
        )


class TestFailJobAbsentFallback1258:
    """Bug #1258: fail_job absent-from-memory persisted-row fallback."""

    def test_fail_job_already_failed_in_db_logs_debug_not_warning(
        self, tracker, caplog
    ):
        """Benign double-dispatch on the failure path: DB row already
        'failed' -- second fail_job call must log DEBUG, not WARNING."""
        tracker.register_job("job-dbl-fail-001", "description_refresh", "system")
        tracker.update_status("job-dbl-fail-001", status="running")
        tracker.fail_job("job-dbl-fail-001", error="first error")

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.fail_job("job-dbl-fail-001", error="second error")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [], (
            f"Expected no WARNING, got: {[r.message for r in warnings]}"
        )

    def test_fail_job_already_failed_in_db_does_not_overwrite_error(
        self, tracker, db_path
    ):
        """First-terminal-write wins on the failure path too."""
        tracker.register_job("job-dbl-fail-002", "description_refresh", "system")
        tracker.update_status("job-dbl-fail-002", status="running")
        tracker.fail_job("job-dbl-fail-002", error="first error")
        tracker.fail_job("job-dbl-fail-002", error="second error")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs WHERE job_id = ?",
            ("job-dbl-fail-002",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "first error"

    def test_fail_job_already_completed_in_db_does_not_downgrade_to_failed(
        self, tracker, db_path
    ):
        """A redundant fail_job call must NEVER flip an already-completed
        row to failed -- first terminal write wins regardless of which
        terminal method is redundantly invoked."""
        tracker.register_job("job-mixed-terminal-001", "description_refresh", "system")
        tracker.update_status("job-mixed-terminal-001", status="running")
        tracker.complete_job("job-mixed-terminal-001", result={"ok": True})

        tracker.fail_job("job-mixed-terminal-001", error="should not apply")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs WHERE job_id = ?",
            ("job-mixed-terminal-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"
        assert row[1] is None

    def test_fail_job_absent_and_db_row_still_running_forces_terminal_update(
        self, tracker, db_path, caplog
    ):
        """Genuine zombie edge on the failure path: forces a terminal DB
        update and logs at WARNING."""
        tracker.register_job("job-zombie-fail-001", "description_refresh", "system")
        tracker.update_status("job-zombie-fail-001", status="running")

        with tracker._lock:
            del tracker._active_jobs["job-zombie-fail-001"]

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.fail_job("job-zombie-fail-001", error="forced failure")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error, completed_at FROM background_jobs WHERE job_id = ?",
            ("job-zombie-fail-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "forced failure"
        assert row[2] is not None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got: {[r.message for r in warnings]}"
        )

    def test_fail_job_absent_and_no_db_row_logs_warning_and_noops(
        self, tracker, caplog
    ):
        """No DB row at all: logs WARNING and does not raise."""
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.job_tracker"
        ):
            tracker.fail_job("nonexistent-job-with-warning-check", error="irrelevant")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got: {[r.message for r in warnings]}"
        )
