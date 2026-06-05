"""
Tests for per-table cleanup isolation in DataRetentionScheduler (Bug #1068).

Requirement: when one table's cleanup raises an unexpected exception,
the remaining tables must still be cleaned up.  One broken table must
NEVER abort the entire cycle.

Covers both the SQLite and PG cleanup paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(
    operational_logs_retention_hours: int = 168,
    audit_logs_retention_hours: int = 720,
    sync_jobs_retention_hours: int = 168,
    dep_map_history_retention_hours: int = 720,
    background_jobs_retention_hours: int = 24,
) -> Any:
    """Build a minimal config_service stub."""
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = operational_logs_retention_hours
    ret_cfg.audit_logs_retention_hours = audit_logs_retention_hours
    ret_cfg.sync_jobs_retention_hours = sync_jobs_retention_hours
    ret_cfg.dep_map_history_retention_hours = dep_map_history_retention_hours
    ret_cfg.background_jobs_retention_hours = background_jobs_retention_hours
    ret_cfg.cleanup_interval_hours = 1

    config = MagicMock()
    config.data_retention_config = ret_cfg

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_scheduler(
    config_service: Any,
    tmp_path: Path,
    storage_mode: str = "sqlite",
    backend_registry: Any = None,
) -> Any:
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    return DataRetentionScheduler(
        log_db_path=tmp_path / "logs.db",
        main_db_path=tmp_path / "main.db",
        groups_db_path=tmp_path / "groups.db",
        config_service=config_service,
        storage_mode=storage_mode,
        backend_registry=backend_registry,
    )


# ---------------------------------------------------------------------------
# SQLite path: per-table isolation
# ---------------------------------------------------------------------------


class TestSqlitePerTableIsolation:
    """
    When _cleanup_table raises for one table, the scheduler must still
    invoke _cleanup_table for all remaining tables.
    """

    def test_second_table_failure_does_not_skip_remaining_tables(
        self, tmp_path: Path
    ) -> None:
        """
        Given: _cleanup_table raises RuntimeError on the 'audit_logs' call
               (second table in the SQLite cycle).
        When:  _execute_cleanup_sqlite() is called.
        Then:  'sync_jobs', 'dependency_map_tracking', and 'background_jobs'
               tables are still attempted (not skipped).
        Bug #1068: one broken table must never abort the others.
        """
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        call_log: list[str] = []

        def fake_cleanup_table(
            db_path: Any,
            table_name: str,
            timestamp_col: str,
            retention_hours: int,
            status_filter: Any = None,
        ) -> int:
            call_log.append(table_name)
            if table_name == "audit_logs":
                raise RuntimeError("simulated audit_logs failure")
            return 0

        with patch.object(scheduler, "_cleanup_table", side_effect=fake_cleanup_table):
            # Must not raise — the scheduler absorbs per-table errors
            scheduler._execute_cleanup_sqlite()

        # All 5 table slots must have been attempted
        assert "logs" in call_log, "logs table was skipped after audit_logs failure"
        assert "sync_jobs" in call_log, (
            "sync_jobs table was skipped after audit_logs failure (Bug #1068)"
        )
        assert "dependency_map_tracking" in call_log, (
            "dependency_map_tracking table was skipped after audit_logs failure (Bug #1068)"
        )
        assert "background_jobs" in call_log, (
            "background_jobs table was skipped after audit_logs failure (Bug #1068)"
        )

    def test_first_table_failure_does_not_skip_remaining_tables(
        self, tmp_path: Path
    ) -> None:
        """
        Given: _cleanup_table raises on the very first table ('logs').
        When:  _execute_cleanup_sqlite() is called.
        Then:  all four remaining tables are still attempted.
        """
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        call_log: list[str] = []

        def fake_cleanup_table(
            db_path: Any,
            table_name: str,
            timestamp_col: str,
            retention_hours: int,
            status_filter: Any = None,
        ) -> int:
            call_log.append(table_name)
            if table_name == "logs":
                raise RuntimeError("simulated logs failure")
            return 5

        with patch.object(scheduler, "_cleanup_table", side_effect=fake_cleanup_table):
            scheduler._execute_cleanup_sqlite()

        assert "audit_logs" in call_log
        assert "sync_jobs" in call_log
        assert "dependency_map_tracking" in call_log
        assert "background_jobs" in call_log

    def test_result_dict_records_error_for_failed_table(self, tmp_path: Path) -> None:
        """
        When a table raises, the returned dict must include a 'failed_tables'
        list naming the failing table so callers can observe which tables failed.

        Bug #1068 observability: silent 0-deleted count is NOT sufficient;
        callers need to distinguish "0 rows matched" from "table errored".
        """
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        def fake_cleanup_table(
            db_path: Any,
            table_name: str,
            timestamp_col: str,
            retention_hours: int,
            status_filter: Any = None,
        ) -> int:
            if table_name == "audit_logs":
                raise RuntimeError("boom")
            return 0

        with patch.object(scheduler, "_cleanup_table", side_effect=fake_cleanup_table):
            result = scheduler._execute_cleanup_sqlite()

        # The result must contain a 'failed_tables' list that names audit_logs
        assert "failed_tables" in result, (
            "Result dict must have 'failed_tables' key to surface per-table errors "
            "(Bug #1068 anti-silent-failure)."
        )
        assert "audit_logs" in result["failed_tables"], (
            f"'audit_logs' must appear in failed_tables, got {result['failed_tables']!r}."
        )
        # Healthy tables must still have integer counts
        assert isinstance(result.get("logs_deleted"), int)
        assert isinstance(result.get("sync_jobs_deleted"), int)

    def test_execute_cleanup_calls_fail_job_when_table_fails(
        self, tmp_path: Path
    ) -> None:
        """
        When any table fails during _execute_cleanup_sqlite(), _execute_cleanup()
        must call fail_job (not complete_job) on the JobTracker so the failure
        is visible in the job outcome — not just the log.

        Bug #1068 anti-silent-failure: calling complete_job when tables errored
        reports SUCCESS even though data may not have been cleaned.
        """
        from unittest.mock import MagicMock

        config_service = _make_config()
        job_tracker = MagicMock()
        scheduler = _make_scheduler(config_service, tmp_path)
        scheduler._job_tracker = job_tracker
        # Patch _execute_cleanup_sqlite to return a result with failed_tables
        scheduler._execute_cleanup_sqlite = MagicMock(  # type: ignore[method-assign]
            return_value={
                "logs_deleted": 0,
                "audit_logs_deleted": 0,
                "sync_jobs_deleted": 0,
                "dep_map_history_deleted": 0,
                "background_jobs_deleted": 0,
                "total_deleted": 0,
                "failed_tables": ["audit_logs"],
            }
        )

        scheduler._execute_cleanup()

        # fail_job must have been called (not complete_job) because audit_logs failed
        job_tracker.fail_job.assert_called_once()
        job_tracker.complete_job.assert_not_called()
        # The error string passed to fail_job must mention the failing table
        call_kwargs = job_tracker.fail_job.call_args
        error_arg = call_kwargs[1].get("error", "") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "audit_logs" in error_arg, (
            f"fail_job error must name the failing table, got: {error_arg!r}."
        )

    def test_all_healthy_tables_still_return_int_counts(self, tmp_path: Path) -> None:
        """
        When one table fails, the other tables' counts in the result dict
        must be integers (not None / exceptions).
        """
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        def fake_cleanup_table(
            db_path: Any,
            table_name: str,
            timestamp_col: str,
            retention_hours: int,
            status_filter: Any = None,
        ) -> int:
            if table_name == "sync_jobs":
                raise ValueError("simulated sync_jobs failure")
            return 7

        with patch.object(scheduler, "_cleanup_table", side_effect=fake_cleanup_table):
            result = scheduler._execute_cleanup_sqlite()

        assert isinstance(result["logs_deleted"], int)
        assert isinstance(result["audit_logs_deleted"], int)
        assert isinstance(result["dep_map_history_deleted"], int)
        assert isinstance(result["background_jobs_deleted"], int)


# ---------------------------------------------------------------------------
# PG path: per-table isolation
# ---------------------------------------------------------------------------


class TestPgPerTableIsolation:
    """
    When one PG backend cleanup method raises, the remaining backend methods
    must still be called.
    """

    def _make_pg_backend_registry(self, failing_backend: str = "audit_log") -> Any:
        """Build a BackendRegistry stub where one backend raises on cleanup."""
        reg = MagicMock()
        reg.logs.cleanup_old_logs.return_value = 3
        reg.audit_log.cleanup_old_logs.return_value = 4
        reg.sync_jobs.cleanup_old_completed.return_value = 2
        reg.dependency_map_tracking.cleanup_old_history.return_value = 1
        reg.background_jobs.cleanup_old_jobs.return_value = 5

        if failing_backend == "audit_log":
            reg.audit_log.cleanup_old_logs.side_effect = RuntimeError(
                "simulated audit_log PG failure"
            )
        elif failing_backend == "logs":
            reg.logs.cleanup_old_logs.side_effect = RuntimeError(
                "simulated logs PG failure"
            )
        elif failing_backend == "sync_jobs":
            reg.sync_jobs.cleanup_old_completed.side_effect = RuntimeError(
                "simulated sync_jobs PG failure"
            )

        return reg

    def test_audit_log_failure_does_not_skip_remaining_pg_tables(
        self, tmp_path: Path
    ) -> None:
        """
        Given: reg.audit_log.cleanup_old_logs raises.
        When:  _execute_cleanup_pg() is called.
        Then:  sync_jobs, dep_map, and background_jobs backends are still called.
        Bug #1068: one broken table must never abort the others.
        """
        reg = self._make_pg_backend_registry(failing_backend="audit_log")
        config_service = _make_config()
        scheduler = _make_scheduler(
            config_service, tmp_path, storage_mode="postgres", backend_registry=reg
        )

        scheduler._execute_cleanup_pg()

        # These must have been called despite audit_log failure
        reg.sync_jobs.cleanup_old_completed.assert_called_once()
        reg.dependency_map_tracking.cleanup_old_history.assert_called_once()
        reg.background_jobs.cleanup_old_jobs.assert_called_once()

    def test_logs_failure_does_not_skip_audit_log_and_rest(
        self, tmp_path: Path
    ) -> None:
        """
        Given: reg.logs.cleanup_old_logs raises (very first PG table).
        When:  _execute_cleanup_pg() is called.
        Then:  audit_log, sync_jobs, dep_map, and background_jobs are still called.
        """
        reg = self._make_pg_backend_registry(failing_backend="logs")
        config_service = _make_config()
        scheduler = _make_scheduler(
            config_service, tmp_path, storage_mode="postgres", backend_registry=reg
        )

        scheduler._execute_cleanup_pg()

        reg.audit_log.cleanup_old_logs.assert_called_once()
        reg.sync_jobs.cleanup_old_completed.assert_called_once()
        reg.dependency_map_tracking.cleanup_old_history.assert_called_once()
        reg.background_jobs.cleanup_old_jobs.assert_called_once()

    def test_pg_result_dict_has_integer_counts_for_healthy_tables(
        self, tmp_path: Path
    ) -> None:
        """
        When sync_jobs raises, the result dict must still have integer counts
        for logs, audit_log, dep_map, and background_jobs.
        """
        reg = self._make_pg_backend_registry(failing_backend="sync_jobs")
        config_service = _make_config()
        scheduler = _make_scheduler(
            config_service, tmp_path, storage_mode="postgres", backend_registry=reg
        )

        result = scheduler._execute_cleanup_pg()

        assert isinstance(result["logs_deleted"], int)
        assert isinstance(result["audit_logs_deleted"], int)
        assert isinstance(result["dep_map_history_deleted"], int)
        assert isinstance(result["background_jobs_deleted"], int)

    def test_pg_result_dict_includes_all_five_keys(self, tmp_path: Path) -> None:
        """
        Even when all backends succeed, the PG result dict must contain all
        five expected keys.
        """
        reg = MagicMock()
        reg.logs.cleanup_old_logs.return_value = 10
        reg.audit_log.cleanup_old_logs.return_value = 5
        reg.sync_jobs.cleanup_old_completed.return_value = 3
        reg.dependency_map_tracking.cleanup_old_history.return_value = 1
        reg.background_jobs.cleanup_old_jobs.return_value = 7

        config_service = _make_config()
        scheduler = _make_scheduler(
            config_service, tmp_path, storage_mode="postgres", backend_registry=reg
        )

        result = scheduler._execute_cleanup_pg()

        expected_keys = {
            "logs_deleted",
            "audit_logs_deleted",
            "sync_jobs_deleted",
            "dep_map_history_deleted",
            "background_jobs_deleted",
            "total_deleted",
        }
        assert expected_keys.issubset(result.keys()), (
            f"Missing keys: {expected_keys - result.keys()}"
        )
