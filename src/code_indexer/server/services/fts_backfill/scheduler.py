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

import os

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.startup.leader_gate import (
    resolve_sweep_leader as _resolve_sweep_leader,
)

from .sweep import run_fts_backfill_cycle

logger = logging.getLogger(__name__)


# EVO-66188 / background-work audit: the leader predicate moved to
# ``server/startup/leader_gate.py`` so the startup sweeps could share it instead
# of growing a fifth copy. Re-exported here unchanged -- this is still the module
# that wires ``leader_check=lambda: resolve_sweep_leader(app.state)``, and its
# existing importers (and their tests) should not have to care that it moved.
resolve_sweep_leader = _resolve_sweep_leader


# Granularity of the sleep loop: check the stop event this often (seconds) so
# stop() is responsive even with a multi-minute tick interval.
_SLEEP_GRANULARITY_SECONDS = 5
# When this node is NOT the elected leader, re-check leadership on this short poll
# instead of the full tick interval, so a failover (or leader-election wiring that
# completes just after startup) is picked up in seconds, not a whole interval.
_LEADER_POLL_SECONDS = 30
# Safe fallbacks when config cannot be read. Kept in sync with FtsBackfillSweepConfig's
# dataclass defaults (batch enqueued cross-pod per tick; ticks paced for a fast drain).
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_TICK_INTERVAL_MINUTES = 5
_DEFAULT_BUILD_TIMEOUT_SECONDS = 1800
# Per-repo attempt cooldown: a repo whose build was enqueued this recently is not
# re-enqueued (throttles a repeatedly-failing repo to at most once per window). Env
# override mirrors the SCIP reconcile's CIDX_SCIP_ATTEMPT_COOLDOWN_SECONDS.
_DEFAULT_ATTEMPT_COOLDOWN_SECONDS = 3600


def _attempt_cooldown_seconds() -> int:
    try:
        return int(
            os.getenv(
                "CIDX_FTS_ATTEMPT_COOLDOWN_SECONDS",
                str(_DEFAULT_ATTEMPT_COOLDOWN_SECONDS),
            )
        )
    except ValueError:
        return _DEFAULT_ATTEMPT_COOLDOWN_SECONDS


class FtsBackfillSweepScheduler:
    """Paced, cluster-safe golden-repo FTS backfill sweep scheduler."""

    OPERATION_TYPE = "fts_backfill_sweep"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        background_job_manager: Optional[Any],
        config_service: Any,
        tick_fn: Callable[..., Any] = run_fts_backfill_cycle,
        leader_check: Callable[[], bool] = lambda: True,
    ) -> None:
        self._golden_repo_manager = golden_repo_manager
        self._background_job_manager = background_job_manager
        self._config_service = config_service
        self._tick_fn = tick_fn
        # Gate tick submission to the elected leader. Defaults to "always leader" so
        # single-node / sqlite (and existing tests) behave exactly as before; the
        # composition root injects the real leader check in cluster (postgres) mode.
        self._leader_check = leader_check
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
        """The leader's discovery/enqueue tick: scan missing-FTS repos and enqueue a
        per-repo POD_PULL ``fts_backfill_build`` for each (bounded). The builds run on
        whichever pods claim them — this tick never builds anything itself."""
        cfg = self._read_cycle_config()
        counts = self._tick_fn(
            self._golden_repo_manager,
            self._background_job_manager,
            batch_size=cfg["batch_size"],
            cooldown_seconds=_attempt_cooldown_seconds(),
        )
        result = counts.as_dict()
        logger.info("fts_backfill reconcile: %s", result)
        return result

    def _is_leader(self) -> bool:
        """Whether this node should run the sweep. Fail-open (return True) on a
        leader-check glitch: a transient error must never silently stop the sweep."""
        try:
            return bool(self._leader_check())
        except Exception:  # noqa: BLE001 -- fail-open, never let a glitch stop the sweep
            return True

    def _sleep_chunked(self, total_seconds: int) -> None:
        """Sleep ``total_seconds`` in small chunks so stop() stays responsive."""
        total = max(1, int(total_seconds))
        waited = 0
        while waited < total and not self._stop_event.is_set():
            self._stop_event.wait(min(_SLEEP_GRANULARITY_SECONDS, total - waited))
            waited += _SLEEP_GRANULARITY_SECONDS

    # ---------------------------------------------------------------------- main loop
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = self._read_cycle_config()
            if not (cfg["enabled"] and self._background_job_manager is not None):
                self._sleep_chunked(max(1, int(cfg["interval_minutes"])) * 60)
                continue
            # Leader-gate the WORK (EVO-66445 follow-up). The sweep spawns
            # `cidx index --rebuild-fts-index` subprocesses that CLEAR then rewrite each
            # repo's tantivy_index, so N pods x uvicorn-workers running it concurrently
            # race on the same repo (`Failed to open file for read`, `cidx init:
            # Configuration already exists`) and do Nx the work -- the per-job
            # DuplicateJobError single-flight has a TOCTOU window that lets ticks slip
            # through. Mirrors the SCIP reconcile's leader gate. Non-leaders poll
            # leadership on a short interval so failover is picked up promptly.
            if not self._is_leader():
                self._sleep_chunked(_LEADER_POLL_SECONDS)
                continue
            try:
                self.trigger_now()
            except Exception:  # noqa: BLE001 -- a tick error must not kill the loop
                logger.error(
                    "FtsBackfillSweepScheduler: tick submission failed",
                    exc_info=True,
                )
            self._sleep_chunked(max(1, int(cfg["interval_minutes"])) * 60)
