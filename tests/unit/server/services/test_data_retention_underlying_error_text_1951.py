"""
Tests for Bug #1951: per-table cleanup failures must surface the underlying
exception text, not just the table name.

Investigation (see issue #1951) found the `token_blacklist`/`elevated_sessions`
"Per-table cleanup errors" failure observed on staging solo was a single
historical event on 2026-08-31, coincident with and resolved by the Bug #1758
SQLite connection-level lock-timeout fix (commit 7206eed4) -- 757+ consecutive
`data_retention_cleanup` cycles have completed cleanly on staging solo since,
and the clustered (PostgreSQL) staging target has never failed. The
ISO-8601/type-mismatch hypothesis from the issue is disproven: both backends
store `blacklisted_at`/`elevated_at`/`last_touched_at` as NUMERIC
(SQLite REAL / PG DOUBLE PRECISION), confirmed by direct inspection of the
live schemas on both staging targets.

What IS still true today, regardless of whether the original trigger
recurs, is the diagnosability gap the issue calls out as a required (not
optional) fix: every per-table safe wrapper in DataRetentionScheduler
catches the real exception, logs it, and then discards it -- only the bare
table NAME reaches `failed_tables`, so the job's `error` field can never
say *why* a table failed. These tests prove that gap with a real exception
raised through the real call path (not a symbol/typo failure) and then
require the exception text to reach both the per-cleanup result dict and
the job's `fail_job(error=...)` call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


def _make_config(
    operational_logs_retention_hours: int = 168,
    audit_logs_retention_hours: int = 720,
    sync_jobs_retention_hours: int = 168,
    dep_map_history_retention_hours: int = 720,
    background_jobs_retention_hours: int = 24,
) -> Any:
    """Build a minimal config_service stub (mirrors sibling test files)."""
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = operational_logs_retention_hours
    ret_cfg.audit_logs_retention_hours = audit_logs_retention_hours
    ret_cfg.sync_jobs_retention_hours = sync_jobs_retention_hours
    ret_cfg.dep_map_history_retention_hours = dep_map_history_retention_hours
    ret_cfg.background_jobs_retention_hours = background_jobs_retention_hours
    ret_cfg.cleanup_interval_hours = 1

    config = MagicMock()
    config.data_retention_config = ret_cfg
    config.jwt_expiration_minutes = 10

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
# SQLite path: underlying exception text must reach the result dict
# ---------------------------------------------------------------------------


class TestSqliteUnderlyingErrorTextCaptured:
    def test_token_blacklist_failure_captures_real_exception_text(
        self, tmp_path: Path
    ) -> None:
        """
        Given: TokenBlacklist.prune_expired raises a distinctive exception.
        When:  _execute_cleanup_sqlite() runs.
        Then:  result['failed_table_errors']['token_blacklist'] contains the
               REAL exception text, not just the table name in failed_tables.
        """
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        fake_blacklist = MagicMock()
        fake_blacklist.prune_expired.side_effect = RuntimeError(
            "distinctive-token-blacklist-failure-9f81"
        )

        with patch(
            "code_indexer.server.app.get_token_blacklist",
            return_value=fake_blacklist,
        ):
            result = scheduler._execute_cleanup_sqlite()

        assert "token_blacklist" in result["failed_tables"]
        failed_table_errors = result.get("failed_table_errors", {})
        assert "distinctive-token-blacklist-failure-9f81" in failed_table_errors.get(
            "token_blacklist", ""
        ), (
            "The real exception text must reach the result so the job error "
            "can surface it, not just the table name (Bug #1951)."
        )

    def test_elevated_sessions_failure_captures_real_exception_text(
        self, tmp_path: Path
    ) -> None:
        """Same as above, for elevated_sessions."""
        config_service = _make_config()
        scheduler = _make_scheduler(config_service, tmp_path)

        with patch(
            "code_indexer.server.auth.elevated_session_manager."
            "elevated_session_manager.prune_expired",
            side_effect=RuntimeError("distinctive-elevated-sessions-failure-3c27"),
        ):
            result = scheduler._execute_cleanup_sqlite()

        assert "elevated_sessions" in result["failed_tables"]
        failed_table_errors = result.get("failed_table_errors", {})
        assert "distinctive-elevated-sessions-failure-3c27" in failed_table_errors.get(
            "elevated_sessions", ""
        ), (
            "The real exception text must reach the result so the job error "
            "can surface it, not just the table name (Bug #1951)."
        )

    def test_generic_table_failure_captures_real_exception_text(
        self, tmp_path: Path
    ) -> None:
        """The same gap exists for the five generic tables (_safe_cleanup_table)."""
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
                raise RuntimeError("distinctive-audit-logs-failure-77ab")
            return 0

        with patch.object(scheduler, "_cleanup_table", side_effect=fake_cleanup_table):
            result = scheduler._execute_cleanup_sqlite()

        assert "audit_logs" in result["failed_tables"]
        failed_table_errors = result.get("failed_table_errors", {})
        assert "distinctive-audit-logs-failure-77ab" in failed_table_errors.get(
            "audit_logs", ""
        ), "Generic per-table failures must also surface their real exception text."


# ---------------------------------------------------------------------------
# PG path: underlying exception text must reach the result dict
# ---------------------------------------------------------------------------


class TestPgUnderlyingErrorTextCaptured:
    def test_pg_backend_failure_captures_real_exception_text(
        self, tmp_path: Path
    ) -> None:
        reg = MagicMock()
        reg.logs.cleanup_old_logs.side_effect = RuntimeError(
            "distinctive-pg-logs-failure-4e10"
        )
        reg.audit_log.cleanup_old_logs.return_value = 0
        reg.sync_jobs.cleanup_old_completed.return_value = 0
        reg.dependency_map_tracking.cleanup_old_history.return_value = 0
        reg.background_jobs.cleanup_old_jobs.return_value = 0

        config_service = _make_config()
        scheduler = _make_scheduler(
            config_service, tmp_path, storage_mode="postgres", backend_registry=reg
        )

        result = scheduler._execute_cleanup_pg()

        assert "logs" in result["failed_tables"]
        failed_table_errors = result.get("failed_table_errors", {})
        assert "distinctive-pg-logs-failure-4e10" in failed_table_errors.get(
            "logs", ""
        ), "PG backend failures must surface their real exception text too."


# ---------------------------------------------------------------------------
# Job-level: fail_job's error message must include the underlying text
# ---------------------------------------------------------------------------


class TestFailJobErrorIncludesUnderlyingText:
    def test_fail_job_error_includes_both_tables_underlying_text(
        self, tmp_path: Path
    ) -> None:
        """
        _execute_cleanup() must build a fail_job(error=...) message that
        contains the REAL exception text for every failed table, matching
        the issue's explicit acceptance criterion: "A per-table failure
        surfaces the real error text."
        """
        config_service = _make_config()
        job_tracker = MagicMock()
        scheduler = _make_scheduler(config_service, tmp_path)
        scheduler._job_tracker = job_tracker
        scheduler._execute_cleanup_sqlite = MagicMock(  # type: ignore[method-assign]
            return_value={
                "logs_deleted": 0,
                "audit_logs_deleted": 0,
                "sync_jobs_deleted": 0,
                "dep_map_history_deleted": 0,
                "background_jobs_deleted": 0,
                "token_blacklist_deleted": 0,
                "elevated_sessions_deleted": 0,
                "oidc_state_deleted": 0,
                "total_deleted": 0,
                "failed_tables": ["token_blacklist", "elevated_sessions"],
                "failed_table_errors": {
                    "token_blacklist": "boom-tb-123",
                    "elevated_sessions": "boom-es-456",
                },
            }
        )

        scheduler._execute_cleanup()

        job_tracker.fail_job.assert_called_once()
        call_kwargs = job_tracker.fail_job.call_args
        error_arg = call_kwargs[1].get("error", "") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "boom-tb-123" in error_arg, (
            f"fail_job error must include the underlying token_blacklist "
            f"exception text, got: {error_arg!r}"
        )
        assert "boom-es-456" in error_arg, (
            f"fail_job error must include the underlying elevated_sessions "
            f"exception text, got: {error_arg!r}"
        )
        # Table names must still be present too (existing behaviour preserved)
        assert "token_blacklist" in error_arg
        assert "elevated_sessions" in error_arg
