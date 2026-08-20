"""EVO-66445: paced, cluster-safe scheduler for the golden-repo FTS backfill sweep.

Mirrors the established scheduler shape in this codebase (e.g. the HNSW orphan
sweep / activated reaper): a daemon thread that, each interval, submits ONE short
tick job through the ``BackgroundJobManager`` (dashboard visibility + cross-worker
single-flight via ``register_job_if_no_conflict`` -> ``DuplicateJobError``). Each
tick builds FTS for up to ``batch_size`` golden repos that currently lack it.

Deliberately leaner than the HNSW sweep: :func:`..sweep.run_fts_backfill_tick` is
idempotent and re-discovers missing repos every tick, so there is NO durable cursor
and NO new state table -- the "cursor" is the presence of the index itself, and the
sweep converges to a cheap no-op once the fleet is complete. Config
(enabled / batch_size / tick_interval_minutes) is re-read every cycle so runtime
Web-UI changes take effect without a restart; config reads are fail-open so a
transient glitch never silently stops the sweep.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.utils.subprocess_env import build_cidx_subprocess_env

from .sweep import run_fts_backfill_tick

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check the stop event this often (seconds) so
# stop() is responsive even with a multi-minute tick interval.
_SLEEP_GRANULARITY_SECONDS = 5
# Safe fallbacks when config cannot be read.
_DEFAULT_BATCH_SIZE = 10
_DEFAULT_TICK_INTERVAL_MINUTES = 15
_DEFAULT_BUILD_TIMEOUT_SECONDS = 1800


class FtsBackfillSweepScheduler:
    """Paced, cluster-safe golden-repo FTS backfill sweep scheduler."""

    OPERATION_TYPE = "fts_backfill_sweep"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        background_job_manager: Optional[Any],
        config_service: Any,
        tick_fn: Callable[..., Any] = run_fts_backfill_tick,
        subprocess_env_provider: Callable[
            [], Optional[dict]
        ] = build_cidx_subprocess_env,
    ) -> None:
        self._golden_repo_manager = golden_repo_manager
        self._background_job_manager = background_job_manager
        self._config_service = config_service
        self._tick_fn = tick_fn
        self._subprocess_env_provider = subprocess_env_provider
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="FtsBackfillSweepScheduler"
        )
        self._thread.start()
        logger.info("FtsBackfillSweepScheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("FtsBackfillSweepScheduler stopped")

    # ------------------------------------------------------------- config (fail-open)
    def _read_cycle_config(self) -> Dict[str, Any]:
        try:
            cfg = self._config_service.get_config().fts_backfill_sweep_config
            return {
                "enabled": bool(cfg.enabled),
                "batch_size": int(cfg.batch_size),
                "interval_minutes": int(cfg.tick_interval_minutes),
                "build_timeout_seconds": int(cfg.build_timeout_seconds),
            }
        except Exception as exc:  # noqa: BLE001 -- never let a config glitch stop the sweep
            logger.warning(
                "FtsBackfillSweepScheduler: config read failed, using defaults: %s",
                exc,
            )
            return {
                "enabled": True,
                "batch_size": _DEFAULT_BATCH_SIZE,
                "interval_minutes": _DEFAULT_TICK_INTERVAL_MINUTES,
                "build_timeout_seconds": _DEFAULT_BUILD_TIMEOUT_SECONDS,
            }

    # --------------------------------------------------------------- tick submission
    def trigger_now(self) -> Optional[str]:
        """Submit one tick job immediately; returns job_id, or None when another
        worker already claimed this tick (``DuplicateJobError`` -- benign)."""
        assert self._background_job_manager is not None, (
            "trigger_now() requires a real background_job_manager"
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
                "FtsBackfillSweepScheduler: tick already claimed by another "
                "worker; skipping"
            )
            return None
        logger.info("FtsBackfillSweepScheduler: triggered tick (job_id=%s)", job_id)
        return job_id

    # --------------------------------------------------------------- tick execution
    def _run_tick(self) -> Dict[str, Any]:
        cfg = self._read_cycle_config()
        stats = self._tick_fn(
            self._golden_repo_manager,
            batch_size=cfg["batch_size"],
            subprocess_env=self._subprocess_env_provider(),
            timeout=cfg["build_timeout_seconds"],
        )
        return {
            "total_missing": stats.total_missing,
            "attempted": stats.attempted,
            "rebuilt": stats.rebuilt,
            "failed": stats.failed,
            "remaining": stats.remaining_after_tick,
        }

    # ---------------------------------------------------------------------- main loop
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = self._read_cycle_config()
            if cfg["enabled"] and self._background_job_manager is not None:
                try:
                    self.trigger_now()
                except Exception:  # noqa: BLE001 -- a tick error must not kill the loop
                    logger.error(
                        "FtsBackfillSweepScheduler: tick submission failed",
                        exc_info=True,
                    )
            # Sleep the configured interval in small chunks so stop() is prompt.
            total = max(1, int(cfg["interval_minutes"])) * 60
            waited = 0
            while waited < total and not self._stop_event.is_set():
                self._stop_event.wait(min(_SLEEP_GRANULARITY_SECONDS, total - waited))
                waited += _SLEEP_GRANULARITY_SECONDS
