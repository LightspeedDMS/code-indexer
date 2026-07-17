"""Embedding & reranker call tracking retention sweep scheduler (Story
#1418 Phase 3 Component 9).

Reuses the exact simple ActivatedReaperScheduler template (Story #967) --
the simplest matching scheduler shape in this codebase (no durable cursor /
multi-tick pass, unlike the HNSW orphan sweep, since a single
DELETE ... WHERE occurred_at < cutoff sweep is idempotent and cheap to
re-run in full every tick). A periodic tick submits ONE short
BackgroundJobManager job that calls backend.delete_where(occurred_at_before)
for rows older than embedding_stats_config.retention_days, respecting the
enabled kill-switch (Component 6) -- a disabled config skips deletion
entirely rather than deleting nothing via a zero-width window.
"""

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check stop_event this often (seconds) --
# mirrors ActivatedReaperScheduler's _TICK_SECONDS pattern.
_TICK_SECONDS = 60

# Fixed tick cadence: retention cleanup is cheap and idempotent, no Web UI
# tunable needed for this (unlike enabled/retention_days, which ARE
# Web-UI-configurable via EmbeddingStatsConfig -- Component 6).
_TICK_INTERVAL_HOURS = 6
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


class EmbeddingStatsRetentionScheduler:
    """
    Daemon scheduler that periodically submits an embedding-stats retention
    sweep job.

    The scheduler submits a BackgroundJobManager job on each tick so cycles
    appear in the job dashboard (Background Jobs Checklist, CLAUDE.md).
    enabled/retention_days are re-read from config_service on every
    _run_tick() invocation so Web UI changes are honoured without a
    restart.
    """

    OPERATION_TYPE = "embedding_stats_retention_sweep"

    def __init__(
        self,
        backend: Any,
        background_job_manager: Any,
        config_service: Any,
    ) -> None:
        """
        Args:
            backend: EmbeddingCallStatsSqliteBackend or
                EmbeddingCallStatsPostgresBackend with delete_where().
            background_job_manager: BackgroundJobManager with submit_job().
            config_service: ConfigService with get_config() returning a
                ServerConfig exposing embedding_stats_config
                (enabled, retention_days).
        """
        self._backend = backend
        self._background_job_manager = background_job_manager
        self._config_service = config_service

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread. A sweep job is submitted on the first tick."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="EmbeddingStatsRetentionScheduler",
        )
        self._thread.start()
        logger.info("EmbeddingStatsRetentionScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("EmbeddingStatsRetentionScheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    def trigger_now(self) -> Optional[str]:
        """
        Submit a retention sweep job immediately, independent of the tick timer.

        Returns:
            job_id returned by background_job_manager.submit_job(), or None
            when another worker already claimed this tick (DuplicateJobError)
            -- benign and expected in multi-worker deployments.
        """
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError,
        )

        try:
            job_id: str = self._background_job_manager.submit_job(
                self.OPERATION_TYPE,
                self._run_tick,
                submitter_username="system",
                is_admin=True,
                repo_alias="server",
            )
        except DuplicateJobError:
            logger.debug(
                "EmbeddingStatsRetentionScheduler: sweep already claimed by "
                "another worker; skipping"
            )
            return None

        logger.info(
            "EmbeddingStatsRetentionScheduler: triggered sweep (job_id=%s)", job_id
        )
        return job_id

    # ------------------------------------------------------------------
    # Tick logic
    # ------------------------------------------------------------------

    def _run_tick(self) -> None:
        """One sweep cycle: delete rows older than the retention cutoff,
        respecting the enabled kill-switch. Config is read fresh on EVERY
        call (never cached), so a Web UI change to enabled/retention_days
        takes effect on the next tick without a restart. Fail-open on
        config read failure (skips deletion rather than guessing a
        cutoff)."""
        try:
            stats_cfg = self._config_service.get_config().embedding_stats_config
        except Exception as exc:
            logger.warning(
                "EmbeddingStatsRetentionScheduler: failed to read config, "
                "skipping this cycle: %s",
                exc,
            )
            return

        if not stats_cfg.enabled:
            logger.debug(
                "EmbeddingStatsRetentionScheduler: embedding stats disabled, "
                "skipping retention sweep"
            )
            return

        cutoff = time.time() - (stats_cfg.retention_days * _SECONDS_PER_DAY)
        deleted = self._backend.delete_where(cutoff)
        logger.info(
            "EmbeddingStatsRetentionScheduler: deleted %d rows older than %d days",
            deleted,
            stats_cfg.retention_days,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: submit a sweep job, then wait for the fixed tick cadence."""
        while not self._stop_event.is_set():
            try:
                self.trigger_now()
            except Exception as exc:
                logger.error(
                    "EmbeddingStatsRetentionScheduler: error submitting sweep job: %s",
                    exc,
                    exc_info=True,
                )

            cadence_seconds = _TICK_INTERVAL_HOURS * _SECONDS_PER_HOUR
            elapsed = 0
            while elapsed < cadence_seconds and not self._stop_event.is_set():
                self._stop_event.wait(timeout=_TICK_SECONDS)
                elapsed += _TICK_SECONDS
