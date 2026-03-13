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
from typing import Any, Optional

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
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            log_db_path:    Path to logs.db
            main_db_path:   Path to cidx_server.db
            groups_db_path: Path to groups.db
            config_service: Object with get_config() returning a config with
                            data_retention_config attribute.
            job_tracker:    Optional JobTracker for unified job tracking.
        """
        self._log_db_path = Path(log_db_path)
        self._main_db_path = Path(main_db_path)
        self._groups_db_path = Path(groups_db_path)
        self._config_service = config_service
        self._job_tracker = job_tracker

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
                logger.error("DataRetentionScheduler: unexpected error in cleanup: %s", e, exc_info=True)

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
            cfg = self._config_service.get_config().data_retention_config

            logs_deleted = self._cleanup_table(
                self._log_db_path,
                "logs",
                "timestamp",
                retention_hours=cfg.operational_logs_retention_hours,
            )

            audit_logs_deleted = self._cleanup_table(
                self._groups_db_path,
                "audit_logs",
                "timestamp",
                retention_hours=cfg.audit_logs_retention_hours,
            )

            sync_jobs_deleted = self._cleanup_table(
                self._main_db_path,
                "sync_jobs",
                "completed_at",
                retention_hours=cfg.sync_jobs_retention_hours,
                status_filter="status IN ('completed', 'failed')",
            )

            dep_map_history_deleted = self._cleanup_dep_map_history(cfg)

            background_jobs_deleted = self._cleanup_table(
                self._main_db_path,
                "background_jobs",
                "completed_at",
                retention_hours=cfg.background_jobs_retention_hours,
                status_filter="status IN ('completed', 'failed', 'cancelled')",
            )

            result = {
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
            }

            logger.info(
                "DataRetentionScheduler: cleanup complete %s", result
            )

            if self._job_tracker is not None:
                self._job_tracker.complete_job(job_id, result=result)

        except Exception as e:
            logger.error("DataRetentionScheduler: cleanup failed: %s", e, exc_info=True)
            if self._job_tracker is not None:
                self._job_tracker.fail_job(job_id, error=str(e))

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
