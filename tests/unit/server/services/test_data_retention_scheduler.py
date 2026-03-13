"""
Tests for DataRetentionScheduler (Story #401).

TDD: These tests were written BEFORE the production code.
Tests cover all acceptance criteria:
- AC1: DataRetentionScheduler daemon thread lifecycle
- AC2: Five tables truncated per retention config
- AC3: Batched DELETEs (1000 rows per batch)
- AC4: Job tracking via JobTracker
- AC5: Startup/shutdown lifecycle
- AC6: Config reload without restart
- AC7: No VACUUM executed
"""

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _iso_ago(hours: float) -> str:
    """Return ISO-8601 UTC timestamp `hours` ago."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _iso_future(hours: float) -> str:
    """Return ISO-8601 UTC timestamp `hours` in the future."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class _FakeDataRetentionConfig:
    operational_logs_retention_hours: int = 168
    audit_logs_retention_hours: int = 2160
    sync_jobs_retention_hours: int = 720
    dep_map_history_retention_hours: int = 2160
    background_jobs_retention_hours: int = 720
    cleanup_interval_hours: int = 1


class _FakeServerConfig:
    def __init__(self, retention_cfg=None):
        self.data_retention_config = retention_cfg or _FakeDataRetentionConfig()


class _FakeConfigService:
    def __init__(self, retention_cfg=None):
        self._cfg = _FakeServerConfig(retention_cfg)

    def get_config(self):
        return self._cfg


def _make_fake_job_tracker():
    """Return a MagicMock with the JobTracker interface."""
    jt = MagicMock()
    return jt


def _create_logs_db(path: Path) -> None:
    """Create logs.db with the logs table schema."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                correlation_id TEXT,
                user_id TEXT,
                request_path TEXT,
                extra_data TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        conn.commit()


def _create_groups_db(path: Path) -> None:
    """Create groups.db with the audit_logs table schema."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                admin_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                details TEXT
            )
            """
        )
        conn.commit()


def _create_main_db(path: Path) -> None:
    """Create cidx_server.db with sync_jobs, background_jobs, dependency_map_tracking."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_jobs (
                job_id TEXT PRIMARY KEY,
                username TEXT,
                user_alias TEXT,
                job_type TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS background_jobs (
                job_id TEXT PRIMARY KEY,
                operation_type TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dependency_map_tracking (
                id INTEGER PRIMARY KEY,
                last_run TEXT,
                next_run TEXT,
                status TEXT DEFAULT 'pending',
                commit_hashes TEXT,
                error_message TEXT
            )
            """
        )
        conn.commit()


def _insert_logs(path: Path, count: int, age_hours: float) -> None:
    ts = _iso_ago(age_hours)
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO logs (timestamp, level, source, message) VALUES (?, 'INFO', 'test', 'msg')",
            [(ts,)] * count,
        )
        conn.commit()


def _insert_audit_logs(path: Path, count: int, age_hours: float) -> None:
    ts = _iso_ago(age_hours)
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, 'admin', 'create', 'repo', 'r1')",
            [(ts,)] * count,
        )
        conn.commit()


def _insert_sync_jobs(path: Path, count: int, age_hours: float, status: str = "completed") -> None:
    ts = _iso_ago(age_hours)
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO sync_jobs (job_id, status, completed_at) VALUES (?, ?, ?)",
            [(str(uuid.uuid4()), status, ts) for _ in range(count)],
        )
        conn.commit()


def _insert_background_jobs(
    path: Path, count: int, age_hours: float, status: str = "completed"
) -> None:
    ts = _iso_ago(age_hours)
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO background_jobs (job_id, status, completed_at) VALUES (?, ?, ?)",
            [(str(uuid.uuid4()), status, ts) for _ in range(count)],
        )
        conn.commit()


def _insert_dep_map_tracking(path: Path, count: int, age_hours: float) -> None:
    ts = _iso_ago(age_hours)
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO dependency_map_tracking (last_run, status) VALUES (?, 'completed')",
            [(ts,)] * count,
        )
        conn.commit()


def _count_rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0]
    finally:
        conn.close()


@pytest.fixture()
def db_paths(tmp_path):
    """Set up three databases and return their paths."""
    log_db = tmp_path / "logs.db"
    groups_db = tmp_path / "groups.db"
    main_db = tmp_path / "cidx_server.db"

    _create_logs_db(log_db)
    _create_groups_db(groups_db)
    _create_main_db(main_db)

    return log_db, groups_db, main_db


@pytest.fixture()
def config_service():
    return _FakeConfigService()


@pytest.fixture()
def job_tracker():
    return _make_fake_job_tracker()


@pytest.fixture()
def scheduler(db_paths, config_service, job_tracker):
    """Create a DataRetentionScheduler with real databases."""
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    log_db, groups_db, main_db = db_paths
    sched = DataRetentionScheduler(
        log_db_path=log_db,
        main_db_path=main_db,
        groups_db_path=groups_db,
        config_service=config_service,
        job_tracker=job_tracker,
    )
    yield sched
    # Ensure thread is stopped after each test
    if sched._running:
        sched.stop()


# ---------------------------------------------------------------------------
# AC1: Constructor and lifecycle
# ---------------------------------------------------------------------------


class TestDataRetentionSchedulerConstruction:
    def test_can_be_constructed_with_required_parameters(self, db_paths, config_service):
        """AC1: DataRetentionScheduler can be instantiated with required args."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths
        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
        )
        assert sched is not None
        assert not sched._running

    def test_job_tracker_is_optional(self, db_paths, config_service):
        """AC1: job_tracker parameter is optional (defaults to None)."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths
        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
        )
        assert sched._job_tracker is None


class TestDataRetentionSchedulerLifecycle:
    def test_start_sets_running_true(self, scheduler):
        """AC1: start() sets _running = True."""
        scheduler.start()
        assert scheduler._running is True
        scheduler.stop()

    def test_start_creates_daemon_thread(self, scheduler):
        """AC1: start() creates a daemon thread."""
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        scheduler.stop()

    def test_stop_sets_running_false(self, scheduler):
        """AC1: stop() sets _running = False."""
        scheduler.start()
        scheduler.stop()
        assert scheduler._running is False

    def test_stop_joins_thread(self, scheduler):
        """AC1: stop() waits for thread to terminate."""
        scheduler.start()
        scheduler.stop()
        assert not scheduler._thread.is_alive()

    def test_stop_without_start_is_safe(self, scheduler):
        """AC1: stop() on a non-started scheduler does not raise."""
        scheduler.stop()  # Should not raise

    def test_start_stop_multiple_times_is_safe(self, scheduler):
        """AC1: Calling start/stop multiple times should work."""
        scheduler.start()
        scheduler.stop()
        # Verify not running after stop
        assert scheduler._running is False


# ---------------------------------------------------------------------------
# AC2 + AC3: Cleanup table logic
# ---------------------------------------------------------------------------


class TestCleanupTableBasic:
    def test_deletes_old_logs_records(self, scheduler, db_paths):
        """AC2: Old logs records are deleted based on retention hours."""
        log_db, groups_db, main_db = db_paths
        # Insert 5 records older than retention (200h > 168h default)
        _insert_logs(log_db, 5, age_hours=200)
        # Insert 2 records within retention (50h < 168h)
        _insert_logs(log_db, 2, age_hours=50)

        deleted = scheduler._cleanup_table(
            log_db, "logs", "timestamp", retention_hours=168
        )

        assert deleted == 5
        assert _count_rows(log_db, "logs") == 2

    def test_does_not_delete_recent_logs(self, scheduler, db_paths):
        """AC2: Recent records are preserved."""
        log_db, groups_db, main_db = db_paths
        _insert_logs(log_db, 10, age_hours=10)

        deleted = scheduler._cleanup_table(
            log_db, "logs", "timestamp", retention_hours=168
        )

        assert deleted == 0
        assert _count_rows(log_db, "logs") == 10

    def test_deletes_old_audit_logs(self, scheduler, db_paths):
        """AC2: Old audit_logs records are deleted from groups.db."""
        log_db, groups_db, main_db = db_paths
        _insert_audit_logs(groups_db, 3, age_hours=3000)  # > 2160h retention
        _insert_audit_logs(groups_db, 2, age_hours=100)   # < 2160h retention

        deleted = scheduler._cleanup_table(
            groups_db, "audit_logs", "timestamp", retention_hours=2160
        )

        assert deleted == 3
        assert _count_rows(groups_db, "audit_logs") == 2

    def test_handles_empty_table_gracefully(self, scheduler, db_paths):
        """AC2: Cleanup on empty table returns 0."""
        log_db, groups_db, main_db = db_paths
        deleted = scheduler._cleanup_table(
            log_db, "logs", "timestamp", retention_hours=168
        )
        assert deleted == 0


class TestCleanupTableStatusFilter:
    def test_sync_jobs_only_deletes_completed_and_failed(self, scheduler, db_paths):
        """AC2: sync_jobs cleanup only deletes completed/failed, not running/pending."""
        log_db, groups_db, main_db = db_paths
        _insert_sync_jobs(main_db, 3, age_hours=800, status="completed")
        _insert_sync_jobs(main_db, 2, age_hours=800, status="failed")
        _insert_sync_jobs(main_db, 4, age_hours=800, status="running")
        _insert_sync_jobs(main_db, 1, age_hours=800, status="pending")

        deleted = scheduler._cleanup_table(
            main_db,
            "sync_jobs",
            "completed_at",
            retention_hours=720,
            status_filter="status IN ('completed', 'failed')",
        )

        assert deleted == 5  # 3 completed + 2 failed
        assert _count_rows(main_db, "sync_jobs") == 5  # 4 running + 1 pending

    def test_background_jobs_only_deletes_completed_failed_cancelled(
        self, scheduler, db_paths
    ):
        """AC2: background_jobs cleanup only deletes completed/failed/cancelled."""
        log_db, groups_db, main_db = db_paths
        _insert_background_jobs(main_db, 2, age_hours=800, status="completed")
        _insert_background_jobs(main_db, 1, age_hours=800, status="failed")
        _insert_background_jobs(main_db, 1, age_hours=800, status="cancelled")
        _insert_background_jobs(main_db, 3, age_hours=800, status="running")

        deleted = scheduler._cleanup_table(
            main_db,
            "background_jobs",
            "completed_at",
            retention_hours=720,
            status_filter="status IN ('completed', 'failed', 'cancelled')",
        )

        assert deleted == 4  # 2 completed + 1 failed + 1 cancelled
        assert _count_rows(main_db, "background_jobs") == 3  # running stays

    def test_status_filter_combined_with_age(self, scheduler, db_paths):
        """AC2: Status filter AND age both apply — recent completed jobs are kept."""
        log_db, groups_db, main_db = db_paths
        # Old completed (should be deleted)
        _insert_sync_jobs(main_db, 3, age_hours=800, status="completed")
        # Recent completed (should be kept - within retention)
        _insert_sync_jobs(main_db, 2, age_hours=10, status="completed")

        deleted = scheduler._cleanup_table(
            main_db,
            "sync_jobs",
            "completed_at",
            retention_hours=720,
            status_filter="status IN ('completed', 'failed')",
        )

        assert deleted == 3
        assert _count_rows(main_db, "sync_jobs") == 2


class TestBatchedDeletes:
    def test_deletes_in_batches_of_1000(self, scheduler, db_paths):
        """AC3: Large deletions are processed in batches of 1000."""
        log_db, groups_db, main_db = db_paths
        # Insert 2500 old records to force multiple batches
        _insert_logs(log_db, 2500, age_hours=200)

        deleted = scheduler._cleanup_table(
            log_db, "logs", "timestamp", retention_hours=168
        )

        assert deleted == 2500
        assert _count_rows(log_db, "logs") == 0

    def test_batch_count_accumulates_correctly(self, scheduler, db_paths):
        """AC3: Total count is accumulated across all batches."""
        log_db, groups_db, main_db = db_paths
        # 1001 rows = 2 batches (1000 + 1)
        _insert_logs(log_db, 1001, age_hours=200)

        deleted = scheduler._cleanup_table(
            log_db, "logs", "timestamp", retention_hours=168
        )

        assert deleted == 1001
        assert _count_rows(log_db, "logs") == 0


# ---------------------------------------------------------------------------
# AC2: dependency_map_tracking uses last_run column
# ---------------------------------------------------------------------------


class TestDepMapTrackingCleanup:
    def test_cleanup_dep_map_history_uses_last_run_column(self, scheduler, db_paths):
        """AC2: dependency_map_tracking cleanup uses 'last_run' column."""
        log_db, groups_db, main_db = db_paths
        _insert_dep_map_tracking(main_db, 4, age_hours=3000)  # > 2160h retention
        _insert_dep_map_tracking(main_db, 2, age_hours=100)   # < 2160h retention

        cfg = _FakeDataRetentionConfig()
        deleted = scheduler._cleanup_dep_map_history(cfg)

        assert deleted == 4
        assert _count_rows(main_db, "dependency_map_tracking") == 2

    def test_cleanup_dep_map_history_zero_when_all_recent(self, scheduler, db_paths):
        """AC2: dependency_map_tracking cleanup deletes nothing when all recent."""
        log_db, groups_db, main_db = db_paths
        _insert_dep_map_tracking(main_db, 5, age_hours=10)

        cfg = _FakeDataRetentionConfig()
        deleted = scheduler._cleanup_dep_map_history(cfg)

        assert deleted == 0
        assert _count_rows(main_db, "dependency_map_tracking") == 5


# ---------------------------------------------------------------------------
# AC4: Job tracking
# ---------------------------------------------------------------------------


class TestJobTrackerIntegration:
    def test_execute_cleanup_registers_job_with_tracker(self, scheduler, db_paths, job_tracker):
        """AC4: execute_cleanup registers a job with JobTracker."""
        scheduler._execute_cleanup()

        job_tracker.register_job.assert_called_once()
        call_kwargs = job_tracker.register_job.call_args
        args, kwargs = call_kwargs
        # First positional arg is job_id — check it starts with "data-retention-"
        job_id = args[0] if args else kwargs.get("job_id")
        assert job_id.startswith("data-retention-")

    def test_execute_cleanup_job_id_format(self, scheduler, db_paths, job_tracker):
        """AC4: job_id format is 'data-retention-{8 hex chars}'."""
        scheduler._execute_cleanup()

        args, kwargs = job_tracker.register_job.call_args
        job_id = args[0] if args else kwargs.get("job_id")
        # Format: "data-retention-" + 8 hex chars
        parts = job_id.split("-")
        # "data" + "retention" + 8-char hex
        assert len(job_id) == len("data-retention-") + 8
        assert all(c in "0123456789abcdef" for c in job_id[-8:])

    def test_execute_cleanup_uses_correct_operation_type(self, scheduler, job_tracker):
        """AC4: operation_type is 'data_retention_cleanup'."""
        scheduler._execute_cleanup()

        args, kwargs = job_tracker.register_job.call_args
        op_type = args[1] if len(args) > 1 else kwargs.get("operation_type")
        assert op_type == "data_retention_cleanup"

    def test_execute_cleanup_uses_system_username(self, scheduler, job_tracker):
        """AC4: username is 'system'."""
        scheduler._execute_cleanup()

        args, kwargs = job_tracker.register_job.call_args
        username = kwargs.get("username") or (args[2] if len(args) > 2 else None)
        assert username == "system"

    def test_execute_cleanup_uses_server_repo_alias(self, scheduler, job_tracker):
        """AC4: repo_alias is 'server'."""
        scheduler._execute_cleanup()

        args, kwargs = job_tracker.register_job.call_args
        repo_alias = kwargs.get("repo_alias")
        assert repo_alias == "server"

    def test_execute_cleanup_calls_update_status_running(self, scheduler, job_tracker):
        """AC4: update_status is called with status='running'."""
        scheduler._execute_cleanup()

        job_tracker.update_status.assert_called()
        # Find the call with status='running'
        running_calls = [
            c for c in job_tracker.update_status.call_args_list
            if c.kwargs.get("status") == "running" or (c.args and "running" in c.args)
        ]
        assert len(running_calls) >= 1

    def test_execute_cleanup_calls_complete_job(self, scheduler, job_tracker):
        """AC4: complete_job is called with result dict."""
        scheduler._execute_cleanup()

        job_tracker.complete_job.assert_called_once()
        args, kwargs = job_tracker.complete_job.call_args
        result = kwargs.get("result") or (args[1] if len(args) > 1 else None)
        assert result is not None
        assert "total_deleted" in result

    def test_execute_cleanup_result_contains_all_table_counts(
        self, scheduler, job_tracker
    ):
        """AC4: Result JSON includes per-table deletion counts."""
        scheduler._execute_cleanup()

        args, kwargs = job_tracker.complete_job.call_args
        result = kwargs.get("result") or (args[1] if len(args) > 1 else None)
        assert "logs_deleted" in result
        assert "audit_logs_deleted" in result
        assert "sync_jobs_deleted" in result
        assert "dep_map_history_deleted" in result
        assert "background_jobs_deleted" in result
        assert "total_deleted" in result

    def test_execute_cleanup_total_equals_sum_of_parts(
        self, scheduler, job_tracker, db_paths
    ):
        """AC4: total_deleted == sum of individual table counts."""
        log_db, groups_db, main_db = db_paths
        _insert_logs(log_db, 5, age_hours=200)
        _insert_audit_logs(groups_db, 3, age_hours=3000)

        scheduler._execute_cleanup()

        args, kwargs = job_tracker.complete_job.call_args
        result = kwargs.get("result") or (args[1] if len(args) > 1 else None)
        expected_total = (
            result["logs_deleted"]
            + result["audit_logs_deleted"]
            + result["sync_jobs_deleted"]
            + result["dep_map_history_deleted"]
            + result["background_jobs_deleted"]
        )
        assert result["total_deleted"] == expected_total

    def test_execute_cleanup_without_job_tracker_does_not_raise(
        self, db_paths, config_service
    ):
        """AC4: Scheduler works without a job_tracker (optional dependency)."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths
        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
            job_tracker=None,
        )
        # Should not raise even without job tracker
        sched._execute_cleanup()

    def test_execute_cleanup_calls_fail_job_on_error(
        self, db_paths, config_service, job_tracker
    ):
        """AC4: fail_job is called when cleanup raises an exception."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths
        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
            job_tracker=job_tracker,
        )

        # Patch _cleanup_table to raise
        with patch.object(sched, "_cleanup_table", side_effect=RuntimeError("db error")):
            sched._execute_cleanup()

        job_tracker.fail_job.assert_called_once()
        args, kwargs = job_tracker.fail_job.call_args
        error = kwargs.get("error") or (args[1] if len(args) > 1 else None)
        assert "db error" in error


# ---------------------------------------------------------------------------
# AC6: Config reload per iteration
# ---------------------------------------------------------------------------


class TestConfigReload:
    def test_cleanup_reads_config_from_service(self, scheduler, job_tracker):
        """AC6: _execute_cleanup re-reads config from config_service each call."""
        call_count = [0]
        original_get = scheduler._config_service.get_config

        def counting_get():
            call_count[0] += 1
            return original_get()

        scheduler._config_service.get_config = counting_get

        scheduler._execute_cleanup()
        assert call_count[0] >= 1, "get_config should be called at least once per cleanup"

    def test_changed_retention_takes_effect_on_next_cycle(self, db_paths, job_tracker):
        """AC6: Config changes take effect on next _execute_cleanup call."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths

        # Start with 168h retention, insert logs that are 200h old
        cfg = _FakeDataRetentionConfig()
        cfg.operational_logs_retention_hours = 168
        config_service = _FakeConfigService(cfg)

        _insert_logs(log_db, 5, age_hours=200)

        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
            job_tracker=job_tracker,
        )

        # First cleanup: 168h retention -> 200h-old logs ARE deleted
        sched._execute_cleanup()
        assert _count_rows(log_db, "logs") == 0

        # Re-insert logs 200h old but change retention to 300h
        _insert_logs(log_db, 3, age_hours=200)
        cfg.operational_logs_retention_hours = 300

        # Second cleanup: 300h retention -> 200h-old logs are NOT deleted
        sched._execute_cleanup()
        assert _count_rows(log_db, "logs") == 3


# ---------------------------------------------------------------------------
# AC7: No VACUUM
# ---------------------------------------------------------------------------


class TestNoVacuum:
    def test_cleanup_table_does_not_call_vacuum(self, scheduler, db_paths):
        """AC7: No VACUUM is called in cleanup code.

        Uses MagicMock(wraps=real_conn) so real SQL executes but all
        execute() calls are tracked. Cannot monkey-patch sqlite3.Connection.execute
        directly in CPython 3.9 because it is a read-only C-level descriptor.
        """
        log_db, groups_db, main_db = db_paths
        _insert_logs(log_db, 10, age_hours=200)

        # Obtain the real connection that DatabaseConnectionManager would use
        real_conn = sqlite3.connect(str(log_db), check_same_thread=False)
        wrapped_conn = MagicMock(wraps=real_conn)

        mock_manager = MagicMock()
        mock_manager.get_connection.return_value = wrapped_conn

        with patch(
            "code_indexer.server.services.data_retention_scheduler.DatabaseConnectionManager.get_instance",
            return_value=mock_manager,
        ):
            scheduler._cleanup_table(log_db, "logs", "timestamp", retention_hours=168)

        real_conn.close()

        # Verify no VACUUM was issued in any execute() call
        vacuum_called = [
            call_args
            for call_args in wrapped_conn.execute.call_args_list
            if "VACUUM" in str(call_args).upper()
        ]
        assert len(vacuum_called) == 0, f"VACUUM was called: {vacuum_called}"

    def test_execute_cleanup_does_not_call_vacuum(self, scheduler, db_paths):
        """AC7: No VACUUM is called during full execute_cleanup."""
        vacuum_called = []
        real_connect = sqlite3.connect

        def mock_connect(path, *args, **kwargs):
            conn = real_connect(path, *args, **kwargs)
            original_execute = conn.execute

            def tracked_execute(sql, *a, **kw):
                if "VACUUM" in sql.upper():
                    vacuum_called.append(sql)
                return original_execute(sql, *a, **kw)

            conn.execute = tracked_execute
            return conn

        with patch("sqlite3.connect", side_effect=mock_connect):
            scheduler._execute_cleanup()

        assert len(vacuum_called) == 0, f"VACUUM was called: {vacuum_called}"


# ---------------------------------------------------------------------------
# AC5: Startup behavior (initial cleanup runs immediately)
# ---------------------------------------------------------------------------


class TestStartupBehavior:
    def test_run_loop_executes_cleanup_immediately(self, db_paths, config_service):
        """AC5: Initial cleanup runs immediately without waiting for interval."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        log_db, groups_db, main_db = db_paths
        cleanup_called = threading.Event()

        sched = DataRetentionScheduler(
            log_db_path=log_db,
            main_db_path=main_db,
            groups_db_path=groups_db,
            config_service=config_service,
        )

        original_cleanup = sched._execute_cleanup

        def tracked_cleanup():
            cleanup_called.set()
            original_cleanup()

        sched._execute_cleanup = tracked_cleanup

        # With interval_hours=1, the wait is 3600s — if initial cleanup
        # runs immediately, we see it without waiting the full interval
        sched.start()

        # Should trigger within 2 seconds (not waiting 3600s)
        triggered = cleanup_called.wait(timeout=2.0)
        sched.stop()

        assert triggered, "Initial cleanup did not run immediately on start"

    def test_thread_name_is_data_retention_scheduler(self, scheduler):
        """AC5: Thread has a recognizable name for debugging."""
        scheduler.start()
        assert "DataRetentionScheduler" in scheduler._thread.name
        scheduler.stop()


# ---------------------------------------------------------------------------
# Missing table graceful handling
# ---------------------------------------------------------------------------


class TestMissingTableHandling:
    def test_cleanup_table_handles_missing_table_gracefully(
        self, scheduler, tmp_path
    ):
        """Scheduler handles missing table without crashing (non-existent feature)."""
        empty_db = tmp_path / "empty.db"
        # Create empty database with no tables
        conn = sqlite3.connect(str(empty_db))
        conn.close()

        # Should not raise an unhandled exception — either catches it or table exists
        # The implementation should handle OperationalError for missing table
        try:
            deleted = scheduler._cleanup_table(
                empty_db, "nonexistent_table", "timestamp", retention_hours=168
            )
            # If it doesn't raise, deleted should be 0
            assert deleted == 0
        except Exception as e:
            # Should be caught gracefully at _execute_cleanup level
            pytest.fail(f"_cleanup_table raised unexpected exception: {e}")


# ---------------------------------------------------------------------------
# Bug #435 fix: _cleanup_table must use execute_atomic() per batch
# ---------------------------------------------------------------------------


class TestCleanupTableUsesExecuteAtomic:
    """
    Bug #435 fix: _cleanup_table() must delegate each batch DELETE to
    DatabaseConnectionManager.execute_atomic(), not call conn.execute() +
    conn.commit() directly on the shared thread-local connection.

    Using raw conn.commit() on the shared connection can commit or roll back
    transactions belonging to other callers on the same thread. execute_atomic()
    provides proper transaction isolation for each batch.
    """

    def test_cleanup_table_calls_execute_atomic_not_raw_commit(
        self, scheduler, db_paths
    ) -> None:
        """
        _cleanup_table() must call execute_atomic() on the
        DatabaseConnectionManager instance, not raw conn.commit().
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        log_db, groups_db, main_db = db_paths
        _insert_logs(log_db, 5, age_hours=200)

        atomic_calls: list = []
        real_manager = DatabaseConnectionManager.get_instance(str(log_db))
        real_execute_atomic = real_manager.execute_atomic

        def tracking_execute_atomic(fn):
            atomic_calls.append(fn)
            return real_execute_atomic(fn)

        real_manager.execute_atomic = tracking_execute_atomic

        try:
            deleted = scheduler._cleanup_table(
                log_db, "logs", "timestamp", retention_hours=168
            )
        finally:
            # Restore original
            real_manager.execute_atomic = real_execute_atomic

        assert deleted == 5
        assert len(atomic_calls) >= 1, (
            "_cleanup_table() must call execute_atomic() for each batch DELETE; "
            "raw conn.commit() was used instead"
        )

    def test_cleanup_table_does_not_call_conn_commit_directly(
        self, scheduler, db_paths
    ) -> None:
        """
        _cleanup_table() must not call conn.commit() directly on the shared
        connection returned by get_connection(). All commits must go through
        execute_atomic() to ensure proper transaction isolation.

        Uses MagicMock(wraps=real_conn) so real SQL executes but all method
        calls are tracked. Cannot monkey-patch sqlite3.Connection.commit
        directly in CPython 3.9 because it is a read-only C-level descriptor.
        """
        log_db, groups_db, main_db = db_paths
        _insert_logs(log_db, 3, age_hours=200)

        # Obtain the real connection that DatabaseConnectionManager would use
        real_conn = sqlite3.connect(str(log_db), check_same_thread=False)
        wrapped_conn = MagicMock(wraps=real_conn)

        mock_manager = MagicMock()
        mock_manager.get_connection.return_value = wrapped_conn

        with patch(
            "code_indexer.server.services.data_retention_scheduler.DatabaseConnectionManager.get_instance",
            return_value=mock_manager,
        ):
            deleted = scheduler._cleanup_table(
                log_db, "logs", "timestamp", retention_hours=168
            )

        real_conn.close()

        # Verify no direct commit() was issued via the shared connection
        commit_called = wrapped_conn.commit.call_count
        assert commit_called == 0, (
            f"_cleanup_table() called conn.commit() directly {commit_called} time(s); "
            "it must use execute_atomic() instead"
        )
