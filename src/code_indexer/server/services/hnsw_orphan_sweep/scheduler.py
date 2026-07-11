"""HNSW orphan repair fleet sweep scheduler (Story #1360, Epic #1333 S3).

Component 3 of the discovery mechanism: the paced/resumable job that
composes Component 1/2 discovery (discovery.py) and the per-item
check+repair executor (repair_executor.py) into a durable, cluster-safe
background sweep.

Dashboard pattern (settled 2026-07-11, see the issue's AC4 section): ONE
short BackgroundJobManager/JobTracker job PER TICK -- mirrors
``ActivatedReaperScheduler.trigger_now()`` exactly. The multi-tick PASS is
never itself a job (the job-tracker model auto-force-fails anything running
past a 24h stale threshold and unconditionally kills running/pending jobs on
restart -- a multi-day job would break against both). Cross-pass accumulated
stats live in the durable state backend, read via ``get_stats()`` --
independent of JobTracker, exposed on the admin stats surface.

Cluster correctness (AC3): single-flight ONLY via
``register_job_if_no_conflict`` (through ``background_job_manager.submit_job``,
identical to every other scheduler in this codebase). Deliberately NOT
filtered by ``ShardOwnership.owns()`` -- see discovery.py's module docstring
for why that would create a coverage gap under this story's model.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    enumerate_sweep_candidates,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
    process_candidate,
)

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check stop_event this often (seconds) --
# mirrors ActivatedReaperScheduler's _TICK_SECONDS pattern.
_TICK_SECONDS = 60

# Poll cadence used while the sweep is disabled in config, so re-enabling it
# takes effect promptly without a server restart.
_DISABLED_POLL_SECONDS = 60

# Safe fallback cadence when config cannot be read.
_DEFAULT_TICK_INTERVAL_MINUTES = 7
_DEFAULT_BATCH_SIZE = 15


class HNSWOrphanRepairSweepScheduler:
    """Paced, resumable, cluster-safe HNSW fleet orphan-repair sweep.

    Each tick: claim the single global tick job via
    ``register_job_if_no_conflict`` (through
    ``background_job_manager.submit_job``), enumerate candidates in stable
    sort-key order, process up to ``batch_size`` items whose key is greater
    than the durable cursor, persisting the cursor after EACH item. On
    exhaustion (no key greater than the cursor across the full current
    enumeration), record pass stats and start a new pass.
    """

    OPERATION_TYPE = "hnsw_orphan_repair_sweep"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        activated_repo_manager: Any,
        state_backend: Any,
        background_job_manager: Optional[Any],
        config_service: Any,
        process_fn: Callable[[Any], SweepOutcome] = process_candidate,
    ) -> None:
        """
        Args:
            golden_repo_manager: Object satisfying discovery.py's
                golden_repo_manager surface.
            activated_repo_manager: Object satisfying discovery.py's
                activated_repo_manager surface.
            state_backend: HNSWOrphanSweepStateSqliteBackend or
                HNSWOrphanSweepStatePostgresBackend instance (durable cursor
                + pass stats).
            background_job_manager: BackgroundJobManager instance used to
                submit one short job per tick (dashboard visibility +
                cross-worker single-flight). May be None only when the
                scheduler is used purely for direct ``_run_tick()`` calls in
                tests -- ``trigger_now()``/``start()`` require a real one.
            config_service: Object with ``get_config()`` returning a config
                exposing ``hnsw_orphan_repair_sweep_config`` (enabled,
                batch_size, tick_interval_minutes).
            process_fn: Injectable per-item processor (defaults to the real
                ``process_candidate``); tests may inject a spy/fake.
        """
        self._golden_repo_manager = golden_repo_manager
        self._activated_repo_manager = activated_repo_manager
        self._state_backend = state_backend
        self._background_job_manager = background_job_manager
        self._config_service = config_service
        self._process_fn = process_fn

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="HNSWOrphanRepairSweepScheduler",
        )
        self._thread.start()
        logger.info("HNSWOrphanRepairSweepScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("HNSWOrphanRepairSweepScheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger / tick job submission
    # ------------------------------------------------------------------

    def trigger_now(self) -> Optional[str]:
        """Submit one tick job immediately.

        Returns:
            job_id, or None when another worker already claimed this tick
            (DuplicateJobError) -- benign and expected under multi-worker
            deployments, mirroring every other scheduler in this codebase.
        """
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
                "HNSWOrphanRepairSweepScheduler: tick already claimed by "
                "another worker; skipping"
            )
            return None

        logger.info(
            "HNSWOrphanRepairSweepScheduler: triggered tick (job_id=%s)", job_id
        )
        return job_id

    # ------------------------------------------------------------------
    # Tick execution
    # ------------------------------------------------------------------

    def _batch_size(self) -> int:
        try:
            return int(
                self._config_service.get_config().hnsw_orphan_repair_sweep_config.batch_size
            )
        except Exception as exc:
            logger.warning(
                "HNSWOrphanRepairSweepScheduler: failed to read batch_size from "
                "config, using default %d: %s",
                _DEFAULT_BATCH_SIZE,
                exc,
            )
            return _DEFAULT_BATCH_SIZE

    def _run_tick(self) -> Dict[str, Any]:
        """Process up to ``batch_size`` candidates beyond the durable
        cursor, persisting progress after EACH item. Returns per-tick
        outcome counts."""
        batch_size = self._batch_size()
        state = self._state_backend.get_state()
        cursor = state["last_completed_key"]

        candidates = sorted(
            enumerate_sweep_candidates(
                self._golden_repo_manager, self._activated_repo_manager
            ),
            key=lambda c: c.sort_key,
        )
        pending = [c for c in candidates if cursor is None or c.sort_key > cursor]
        batch = pending[:batch_size]

        counts = {
            SweepOutcome.CLEAN.value: 0,
            SweepOutcome.REPAIRED.value: 0,
            SweepOutcome.TRANSIENT_SKIP.value: 0,
            SweepOutcome.ERROR.value: 0,
        }

        for candidate in batch:
            outcome = self._process_one(candidate)
            counts[outcome.value] += 1
            self._state_backend.record_item_processed(candidate.sort_key, outcome.value)

        # Pass is complete when this tick's batch consumed the ENTIRE
        # pending list -- i.e. no candidate remains whose key is greater
        # than the new cursor. `pending` is the untruncated list (before
        # the batch_size slice), so `len(pending) <= batch_size` means
        # everything pending was just processed.
        if len(pending) <= batch_size:
            self._state_backend.complete_pass()
            logger.info("HNSWOrphanRepairSweepScheduler: pass complete")

        return {"processed": len(batch), **counts}

    def _process_one(self, candidate: Any) -> SweepOutcome:
        """Fail-soft wrapper: any unexpected exception from the per-item
        processor is loud (logged) but counted as ERROR, never aborting the
        tick (AC2: a failure on one index does not abort the pass)."""
        try:
            return self._process_fn(candidate)
        except Exception:
            logger.error(
                "HNSWOrphanRepairSweepScheduler: unexpected error processing %s",
                candidate.sort_key,
                exc_info=True,
            )
            return SweepOutcome.ERROR

    # ------------------------------------------------------------------
    # Admin stats surface (AC4: independent of JobTracker)
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return the durable cross-pass fleet stats (last full pass time,
        total orphans repaired to date, current cursor position) -- backed
        by the same state_backend as the tick cursor, read independently of
        JobTracker."""
        return self._state_backend.get_state()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main loop: submit a tick job (if enabled), then wait for the
        configured interval, repeat. Re-reads enabled/interval from config
        each cycle so Web UI changes take effect without a restart."""
        while not self._stop_event.is_set():
            try:
                cfg = self._config_service.get_config().hnsw_orphan_repair_sweep_config
                enabled = bool(cfg.enabled)
                interval_minutes = int(cfg.tick_interval_minutes)
            except Exception as exc:
                logger.warning(
                    "HNSWOrphanRepairSweepScheduler: failed to read config, "
                    "using defaults: %s",
                    exc,
                )
                enabled = True
                interval_minutes = _DEFAULT_TICK_INTERVAL_MINUTES

            if enabled:
                try:
                    self.trigger_now()
                except Exception as exc:
                    logger.error(
                        "HNSWOrphanRepairSweepScheduler: error submitting tick: %s",
                        exc,
                        exc_info=True,
                    )
                wait_seconds = interval_minutes * 60
            else:
                wait_seconds = _DISABLED_POLL_SECONDS

            elapsed = 0
            while elapsed < wait_seconds and not self._stop_event.is_set():
                self._stop_event.wait(timeout=_TICK_SECONDS)
                elapsed += _TICK_SECONDS
