"""
Data Retention Scheduler (Story #401).

Periodically deletes old records from five tables across three databases
to prevent unbounded growth. Uses batched DELETEs (1000 rows per batch)
to avoid long-running transactions. No VACUUM is performed.
"""

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Rows deleted per batch to avoid long-running transactions
_BATCH_SIZE = 1000


class DataRetentionScheduler:
    """
    Daemon scheduler that periodically purges old records from five tables.

    Tables and their databases:
      - logs             (logs.db)           timestamp col,    no status filter
      - audit_logs       (groups.db)         timestamp col,    no status filter
      - sync_jobs        (cidx_server.db)    completed_at col, status IN ('completed','failed')
      - dependency_map_tracking (cidx_server.db) last_run col, no status filter
      - background_jobs  (cidx_server.db)    completed_at col, status IN ('completed','failed','cancelled')

    Config is re-read from config_service on every cleanup cycle so that
    changes take effect without a server restart.
    """

    def __init__(
        self,
        log_db_path: Path,
        main_db_path: Path,
        groups_db_path: Path,
        config_service: Any,
        job_tracker: Optional[Any] = None,
        storage_mode: str = "sqlite",
        backend_registry: Optional[Any] = None,
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            log_db_path:      Path to logs.db
            main_db_path:     Path to cidx_server.db
            groups_db_path:   Path to groups.db
            config_service:   Object with get_config() returning a config with
                              data_retention_config attribute.
            job_tracker:      Optional JobTracker for unified job tracking.
            storage_mode:     "sqlite" (default) or "postgres". In postgres mode
                              cleanup is delegated to backend protocol methods
                              instead of direct SQLite access.
            backend_registry: BackendRegistry instance required when
                              storage_mode == "postgres".
        """
        self._log_db_path = Path(log_db_path)
        self._main_db_path = Path(main_db_path)
        self._groups_db_path = Path(groups_db_path)
        self._config_service = config_service
        self._job_tracker = job_tracker
        self._storage_mode = storage_mode
        self._backend_registry = backend_registry

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread. Cleanup runs immediately on first iteration."""
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="DataRetentionScheduler",
        )
        self._thread.start()
        logger.info("DataRetentionScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("DataRetentionScheduler stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main loop: run cleanup immediately, then wait interval, repeat."""
        while not self._stop_event.is_set():
            try:
                self._execute_cleanup()
            except Exception as e:
                logger.error(
                    "DataRetentionScheduler: unexpected error in cleanup: %s",
                    e,
                    exc_info=True,
                )

            # Re-read interval from config each iteration
            try:
                cfg = self._config_service.get_config().data_retention_config
                interval_seconds = cfg.cleanup_interval_hours * 3600
            except Exception:
                interval_seconds = 3600  # fallback: 1 hour

            # Wait for interval or until stop is signalled
            self._stop_event.wait(timeout=interval_seconds)

    # ------------------------------------------------------------------
    # Cleanup execution
    # ------------------------------------------------------------------

    def _execute_cleanup(self) -> None:
        """
        Run one cleanup cycle across all five tables.
        Registers the job with JobTracker if available.
        In postgres mode, delegates to _execute_cleanup_pg().
        """
        job_id = f"data-retention-{uuid.uuid4().hex[:8]}"

        if self._job_tracker is not None:
            self._job_tracker.register_job(
                job_id,
                "data_retention_cleanup",
                username="system",
                repo_alias="server",
            )
            self._job_tracker.update_status(job_id, status="running")

        try:
            if self._storage_mode == "postgres" and self._backend_registry is not None:
                result = self._execute_cleanup_pg()
            else:
                result = self._execute_cleanup_sqlite()

            logger.info("DataRetentionScheduler: cleanup complete %s", result)

            if self._job_tracker is not None:
                failed_tables: List[str] = result.get("failed_tables", [])
                if failed_tables:
                    self._job_tracker.fail_job(
                        job_id,
                        error=f"Per-table cleanup errors: {', '.join(failed_tables)}",
                    )
                else:
                    self._job_tracker.complete_job(job_id, result=result)

        except Exception as e:
            logger.error("DataRetentionScheduler: cleanup failed: %s", e, exc_info=True)
            if self._job_tracker is not None:
                self._job_tracker.fail_job(job_id, error=str(e))

    def _execute_cleanup_sqlite(self) -> dict:
        """Run cleanup cycle using direct SQLite access (sqlite mode).

        Each table is cleaned independently — an exception from one table
        is caught, logged, and recorded as 0 rows deleted so that the
        remaining tables are not skipped (Bug #1068).

        Failed tables are collected in the returned dict's 'failed_tables'
        list so the job outcome is set to fail_job when any table errored,
        rather than silently reporting success (Bug #1068 anti-silent-failure).
        """
        cfg = self._config_service.get_config().data_retention_config
        failed_tables: List[str] = []

        logs_deleted = self._safe_cleanup_table(
            self._log_db_path,
            "logs",
            "timestamp",
            retention_hours=cfg.operational_logs_retention_hours,
            failed_tables=failed_tables,
        )

        audit_logs_deleted = self._safe_cleanup_table(
            self._groups_db_path,
            "audit_logs",
            "timestamp",
            retention_hours=cfg.audit_logs_retention_hours,
            failed_tables=failed_tables,
        )

        sync_jobs_deleted = self._safe_cleanup_table(
            self._main_db_path,
            "sync_jobs",
            "completed_at",
            retention_hours=cfg.sync_jobs_retention_hours,
            status_filter="status IN ('completed', 'failed')",
            failed_tables=failed_tables,
        )

        dep_map_history_deleted = self._safe_cleanup_dep_map_history(
            cfg, failed_tables=failed_tables
        )

        background_jobs_deleted = self._safe_cleanup_table(
            self._main_db_path,
            "background_jobs",
            "completed_at",
            retention_hours=cfg.background_jobs_retention_hours,
            status_filter="status IN ('completed', 'failed', 'cancelled')",
            failed_tables=failed_tables,
        )

        return {
            "logs_deleted": logs_deleted,
            "audit_logs_deleted": audit_logs_deleted,
            "sync_jobs_deleted": sync_jobs_deleted,
            "dep_map_history_deleted": dep_map_history_deleted,
            "background_jobs_deleted": background_jobs_deleted,
            "total_deleted": (
                logs_deleted
                + audit_logs_deleted
                + sync_jobs_deleted
                + dep_map_history_deleted
                + background_jobs_deleted
            ),
            "failed_tables": failed_tables,
        }

    def _execute_cleanup_pg(self) -> dict:
        """Run cleanup cycle via backend protocol methods (postgres mode).

        Each backend is called independently — an exception from one is
        caught, logged, and recorded as 0 rows deleted so that the
        remaining backends are not skipped (Bug #1068).

        Failed tables are collected in the returned dict's 'failed_tables'
        list so the job outcome is set to fail_job when any table errored,
        rather than silently reporting success (Bug #1068 anti-silent-failure).
        """
        cfg = self._config_service.get_config().data_retention_config
        reg = self._backend_registry
        failed_tables: List[str] = []

        # LogsBackend.cleanup_old_logs takes days_to_keep
        logs_days = max(1, cfg.operational_logs_retention_hours // 24)
        logs_deleted = self._safe_pg_call(
            "logs",
            lambda: reg.logs.cleanup_old_logs(days_to_keep=logs_days),  # type: ignore[union-attr]
            failed_tables=failed_tables,
        )

        # AuditLogBackend.cleanup_old_logs takes cutoff_iso
        audit_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=cfg.audit_logs_retention_hours)
        ).isoformat()
        audit_logs_deleted = self._safe_pg_call(
            "audit_logs",
            lambda: reg.audit_log.cleanup_old_logs(cutoff_iso=audit_cutoff),  # type: ignore[union-attr]
            failed_tables=failed_tables,
        )

        # SyncJobsBackend.cleanup_old_completed takes cutoff_iso
        sync_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=cfg.sync_jobs_retention_hours)
        ).isoformat()
        sync_jobs_deleted = self._safe_pg_call(
            "sync_jobs",
            lambda: reg.sync_jobs.cleanup_old_completed(cutoff_iso=sync_cutoff),  # type: ignore[union-attr]
            failed_tables=failed_tables,
        )

        # DependencyMapTrackingBackend.cleanup_old_history takes cutoff_iso
        dep_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=cfg.dep_map_history_retention_hours)
        ).isoformat()
        dep_map_history_deleted = self._safe_pg_call(
            "dependency_map_tracking",
            lambda: reg.dependency_map_tracking.cleanup_old_history(  # type: ignore[union-attr]
                cutoff_iso=dep_cutoff
            ),
            failed_tables=failed_tables,
        )

        # BackgroundJobsBackend.cleanup_old_jobs takes max_age_hours
        background_jobs_deleted = self._safe_pg_call(
            "background_jobs",
            lambda: reg.background_jobs.cleanup_old_jobs(  # type: ignore[union-attr]
                max_age_hours=cfg.background_jobs_retention_hours
            ),
            failed_tables=failed_tables,
        )

        return {
            "logs_deleted": logs_deleted,
            "audit_logs_deleted": audit_logs_deleted,
            "sync_jobs_deleted": sync_jobs_deleted,
            "dep_map_history_deleted": dep_map_history_deleted,
            "background_jobs_deleted": background_jobs_deleted,
            "total_deleted": (
                logs_deleted
                + audit_logs_deleted
                + sync_jobs_deleted
                + dep_map_history_deleted
                + background_jobs_deleted
            ),
            "failed_tables": failed_tables,
        }

    def _cleanup_dep_map_history(self, cfg: Any) -> int:
        """
        Clean up old dependency_map_tracking records using the last_run column.
        Separated because the column name differs from the generic pattern.
        """
        return self._cleanup_table(
            self._main_db_path,
            "dependency_map_tracking",
            "last_run",
            retention_hours=cfg.dep_map_history_retention_hours,
        )

    # ------------------------------------------------------------------
    # Per-table safe wrappers (Bug #1068)
    # ------------------------------------------------------------------

    def _safe_cleanup_table(
        self,
        db_path: Path,
        table_name: str,
        timestamp_col: str,
        retention_hours: int,
        status_filter: Optional[str] = None,
        failed_tables: Optional[List[str]] = None,
    ) -> int:
        """
        Call _cleanup_table, catching and logging any exception so that one
        broken table never aborts the cleanup cycle for the others.

        Returns 0 when an exception occurs (same as 'table does not exist').
        When failed_tables is supplied, the table name is appended to it on
        failure so callers can surface per-table errors in the job outcome
        (Bug #1068 anti-silent-failure).
        """
        try:
            return self._cleanup_table(
                db_path,
                table_name,
                timestamp_col,
                retention_hours=retention_hours,
                status_filter=status_filter,
            )
        except Exception as exc:
            logger.error(
                "DataRetentionScheduler: cleanup of table '%s' failed: %s",
                table_name,
                exc,
                exc_info=True,
            )
            if failed_tables is not None:
                failed_tables.append(table_name)
            return 0

    def _safe_cleanup_dep_map_history(
        self, cfg: Any, failed_tables: Optional[List[str]] = None
    ) -> int:
        """
        Per-table-safe wrapper around _cleanup_dep_map_history (Bug #1068).

        When failed_tables is supplied, 'dependency_map_tracking' is appended
        to it on failure so callers can surface the error in the job outcome.
        """
        try:
            return self._cleanup_dep_map_history(cfg)
        except Exception as exc:
            logger.error(
                "DataRetentionScheduler: cleanup of table 'dependency_map_tracking' failed: %s",
                exc,
                exc_info=True,
            )
            if failed_tables is not None:
                failed_tables.append("dependency_map_tracking")
            return 0

    def _safe_pg_call(
        self, table_name: str, call: Any, failed_tables: Optional[List[str]] = None
    ) -> int:
        """
        Execute a single PG backend cleanup call, catching and logging any
        exception so that one broken backend never aborts the cycle (Bug #1068).

        Returns 0 when an exception occurs.
        When failed_tables is supplied, the table name is appended to it on
        failure so callers can surface per-table errors in the job outcome
        (Bug #1068 anti-silent-failure).
        """
        try:
            result: int = call()
            return result if isinstance(result, int) else 0
        except Exception as exc:
            logger.error(
                "DataRetentionScheduler: PG cleanup of '%s' failed: %s",
                table_name,
                exc,
                exc_info=True,
            )
            if failed_tables is not None:
                failed_tables.append(table_name)
            return 0

    # ------------------------------------------------------------------
    # Low-level batched DELETE
    # ------------------------------------------------------------------

    def _cleanup_table(
        self,
        db_path: Path,
        table_name: str,
        timestamp_col: str,
        retention_hours: int,
        status_filter: Optional[str] = None,
    ) -> int:
        """
        Delete rows older than retention_hours from table_name in batches of 1000.

        Args:
            db_path:        Path to the SQLite database file.
            table_name:     Name of the table to purge.
            timestamp_col:  Column storing the ISO-8601 timestamp.
            retention_hours: Rows older than this many hours are deleted.
            status_filter:  Optional SQL condition (e.g. "status IN ('completed')").
                            Combined with age condition via AND.

        Returns:
            Total number of rows deleted.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        ).isoformat()

        age_condition = f"{timestamp_col} < '{cutoff}'"
        if status_filter:
            where_clause = f"({age_condition}) AND ({status_filter})"
        else:
            where_clause = age_condition

        delete_sql = (
            f"DELETE FROM {table_name} "
            f"WHERE rowid IN ("
            f"  SELECT rowid FROM {table_name} "
            f"  WHERE {where_clause} "
            f"  LIMIT {_BATCH_SIZE}"
            f")"
        )

        total_deleted = 0
        manager = DatabaseConnectionManager.get_instance(str(db_path))
        try:
            while True:
                rows_in_batch: list = [0]

                def _do_batch(conn: sqlite3.Connection) -> None:
                    conn.execute(delete_sql)
                    rows_in_batch[0] = conn.execute("SELECT changes()").fetchone()[0]

                manager.execute_atomic(_do_batch)
                if rows_in_batch[0] == 0:
                    break
                total_deleted += rows_in_batch[0]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                logger.debug(
                    "DataRetentionScheduler: table '%s' does not exist in %s, skipping",
                    table_name,
                    db_path,
                )
                return 0
            raise

        return total_deleted
