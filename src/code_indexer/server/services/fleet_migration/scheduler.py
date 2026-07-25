"""FleetMigrationScheduler -- Story #1458 Background Jobs Checklist wiring.

``run_fleet_migration_for_repo()`` (orchestrator.py) is the real, tested
per-repo migration sequence. ``discovery.py`` computes its per-repo
arguments from real golden-repo enumeration. This module is the missing
third piece: the BackgroundJobManager/JobTracker-integrated scheduler that
makes fleet migration a real, dashboard-visible, admin-triggerable
background job, per this project's standing "Background Jobs (MANDATORY
Checklist)" policy (CLAUDE.md) -- every new background job MUST integrate
with BackgroundJobManager + JobTracker for dashboard/admin UI visibility.

Dashboard pattern: reuses the project's EXISTING generic mechanism -- ANY
operation_type submitted via ``background_job_manager.submit_job(...)``
automatically appears in the admin dashboard's recent-jobs panel UNLESS
explicitly added to ``dashboard_service.py``'s
``_DASHBOARD_HIDDEN_OPERATION_TYPES`` list (which this scheduler's
``"fleet_migration"`` operation_type is deliberately NOT added to). No
bespoke dashboard route/template is required -- this mirrors every other
scheduler in this codebase (HNSWOrphanRepairSweepScheduler,
ActivatedReaperScheduler, etc.), so it is the ALREADY-APPROVED frontend
reporting pattern, not a new one requiring separate sign-off.

Fleet-wide (not merely per-repo) single-flight (AC1: "Serialized,
one-repo-at-a-time"): every ``trigger_now()`` submission uses the SAME
FIXED sentinel ``repo_alias`` (``_SCHEDULER_REPO_ALIAS``), so
``register_job_if_no_conflict``'s ``idx_active_job_per_repo`` unique index
-- keyed on ``(operation_type, repo_alias)`` -- rejects a second concurrent
tick regardless of which golden repo it would have picked next. This is the
SAME technique ``HNSWOrphanRepairSweepScheduler`` uses (its own fixed
``repo_alias="server"``) to serialize its whole tick, adapted here to
serialize the whole FLEET rather than one item.

Each job processes exactly ONE golden repo (the migration for a single
repo can legitimately run for a long time -- no per-job timeout, per this
project's "Indexing Path Has No Job/Subprocess/Per-File Timeouts"
invariant) rather than a batch, unlike the HNSW sweep's many-small-items
per tick.

Safety-critical, deliberate divergence from HNSWOrphanRepairSweepScheduler's
config-read fallback: on a config-read failure this scheduler's cycle loop
fails CLOSED (``enabled=False``), never open. Fleet migration deletes real
on-disk sharded files after a verified consolidation; a transient
config-read glitch must never cause an unintended migration to start.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.fleet_migration.completion_gate import (
    invalidate_post_consolidation_snapshot_marker,
)
from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
    is_repo_already_migrated,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    run_fleet_migration_for_repo,
)

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check stop_event this often (seconds) --
# mirrors HNSWOrphanRepairSweepScheduler's _TICK_SECONDS pattern.
_TICK_SECONDS = 60

# Poll cadence used while the scheduler is disabled, so re-enabling it
# (via fleet_migration_config.enabled, editable through the Web UI
# Config Screen's "Fleet Migration" section) takes effect promptly
# without a server restart.
_DISABLED_POLL_SECONDS = 60

_DEFAULT_TICK_INTERVAL_MINUTES = 30

#: Codex round-6 MEDIUM finding: bounded join wait in stop(). A real
#: repro confirmed the worker thread can still be alive after this join
#: returns -- stop() must check thread liveness afterward rather than
#: silently reporting success.
_STOP_JOIN_TIMEOUT_SECONDS = 10


class FleetMigrationScheduler:
    """Paced, resumable, fleet-wide-serialized golden-repo migration
    scheduler.

    Each ``trigger_now()`` call submits AT MOST one job (rejected with
    ``None`` if another migration is already in flight anywhere in the
    fleet). That job re-enumerates candidates (fresh from disk, no separate
    durable cursor -- see discovery.py's module docstring), skips every
    already-migrated repo, and runs the real orchestrator on the first
    pending one.
    """

    OPERATION_TYPE = "fleet_migration"

    #: Fixed dedup key for fleet-wide (not per-repo) single-flight -- see
    #: module docstring.
    _SCHEDULER_REPO_ALIAS = "fleet-migration-scheduler"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        refresh_scheduler: Any,
        background_job_manager: Optional[Any],
        config_service: Any,
    ) -> None:
        """
        Args:
            golden_repo_manager: Object satisfying discovery.py's
                golden_repo_manager surface.
            refresh_scheduler: The REAL RefreshScheduler used by
                run_fleet_migration_for_repo to acquire the write lock and
                fire the AC10 snapshot trigger.
            background_job_manager: BackgroundJobManager instance used to
                submit jobs (dashboard visibility + cross-worker
                single-flight). May be None only for tests that call
                ``_run_next_candidate()`` directly -- ``trigger_now()``/
                ``start()`` require a real one.
            config_service: Object with ``get_config()`` returning a config
                exposing ``fleet_migration_config`` (enabled,
                tick_interval_minutes).
        """
        self._golden_repo_manager = golden_repo_manager
        self._refresh_scheduler = refresh_scheduler
        self._background_job_manager = background_job_manager
        self._config_service = config_service

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
            name="FleetMigrationScheduler",
        )
        self._thread.start()
        logger.info("FleetMigrationScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish.

        Raises:
            RuntimeError: the worker thread was still alive after the
                bounded join wait -- a real, confirmed failure mode this
                method must never silently report as a successful stop.
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                raise RuntimeError(
                    f"FleetMigrationScheduler.stop(): worker thread "
                    f"{self._thread.name!r} did not stop within "
                    f"{_STOP_JOIN_TIMEOUT_SECONDS}s of stop() being "
                    f"called -- it is STILL ALIVE."
                )
        logger.info("FleetMigrationScheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger / job submission
    # ------------------------------------------------------------------

    def trigger_now(self) -> Optional[str]:
        """Submit one per-repo migration job immediately.

        Returns:
            job_id, or None when another migration is already in flight
            anywhere in the fleet (DuplicateJobError on the fixed sentinel
            key) -- benign and expected, mirroring every other scheduler in
            this codebase -- OR when the kill switch is disabled (Codex
            Finding #6: this check must be independent of _loop()'s own
            gate, since trigger_now() is a manual/admin entry point that
            can be reached without ever going through the loop).
        """
        assert self._background_job_manager is not None, (
            "trigger_now() requires a real background_job_manager"
        )
        if not self._is_enabled_now():
            logger.info(
                "FleetMigrationScheduler: trigger_now() refused -- "
                "fleet_migration_config.enabled is False"
            )
            return None
        try:
            job_id: str = self._background_job_manager.submit_job(
                self.OPERATION_TYPE,
                self._run_next_candidate,
                submitter_username="system",
                is_admin=True,
                repo_alias=self._SCHEDULER_REPO_ALIAS,
            )
        except DuplicateJobError:
            logger.debug(
                "FleetMigrationScheduler: a migration is already in flight; "
                "skipping this trigger"
            )
            return None

        logger.info("FleetMigrationScheduler: triggered migration job_id=%s", job_id)
        return job_id

    def _run_next_candidate(self) -> Dict[str, Any]:
        """Enumerate candidates fresh, run the real orchestrator on the
        FIRST not-yet-migrated one found.

        Codex Finding #6: this is the method that actually invokes the
        destructive orchestrator, and it is reachable WITHOUT going through
        trigger_now() (e.g. a job-queue worker invoking the submitted
        callable directly) -- so the kill switch is checked independently
        HERE too, never relying solely on trigger_now()'s own check.
        """
        if not self._is_enabled_now():
            logger.info(
                "FleetMigrationScheduler: _run_next_candidate() refused -- "
                "fleet_migration_config.enabled is False"
            )
            return {"status": "disabled"}

        for candidate in enumerate_fleet_migration_candidates(
            self._golden_repo_manager
        ):
            if is_repo_already_migrated(candidate):
                continue

            # Codex CRITICAL finding (round 4): invalidate a stale marker
            # from a PRIOR migration generation as soon as new
            # unconsolidated work is detected, BEFORE the write lock is
            # even acquired -- so a crash anywhere during this new pass
            # (including before ITS OWN new snapshot fires) leaves the
            # marker durably absent instead of falsely inherited from an
            # earlier generation.
            invalidate_post_consolidation_snapshot_marker(candidate.index_path)

            result = run_fleet_migration_for_repo(
                refresh_scheduler=self._refresh_scheduler,
                sister_alias_manager=candidate.sister_alias_manager,
                repo_alias=candidate.golden_alias,
                base_clone_path=candidate.base_clone_path,
                index_path=candidate.index_path,
                semantic_collection_dirs=candidate.semantic_collection_dirs,
                temporal_namespaces=candidate.temporal_namespaces,
                sister_root=candidate.sister_root,
            )
            logger.info(
                "FleetMigrationScheduler: repo '%s' migration status=%s "
                "(collections_consolidated=%d, temporal_namespaces_processed=%d)",
                candidate.golden_alias,
                result.status,
                result.collections_consolidated,
                result.temporal_namespaces_processed,
            )
            return {
                "golden_alias": candidate.golden_alias,
                "status": result.status,
                "collections_consolidated": result.collections_consolidated,
                "collections_skipped_disk": result.collections_skipped_disk,
                "temporal_namespaces_processed": result.temporal_namespaces_processed,
                "snapshot_path": result.snapshot_path,
                "detail": result.detail,
            }

        return {"status": "nothing_to_migrate"}

    # ------------------------------------------------------------------
    # Admin stats surface
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return live fleet-wide migration counts, derived fresh from
        disk (no separate durable state -- see discovery.py)."""
        candidates = list(
            enumerate_fleet_migration_candidates(self._golden_repo_manager)
        )
        migrated_count = sum(1 for c in candidates if is_repo_already_migrated(c))
        return {
            "total_repos": len(candidates),
            "migrated_repos": migrated_count,
            "pending_repos": len(candidates) - migrated_count,
        }

    def _is_enabled_now(self) -> bool:
        """Codex Finding #6: the kill-switch check shared by EVERY entry
        point that can reach the destructive orchestrator (trigger_now(),
        _run_next_candidate()), not just _loop()'s own scheduled-tick gate.
        Delegates to the same fail-closed config read _loop() uses -- a
        config-read glitch must never be interpreted as "enabled"."""
        return bool(self._read_cycle_config()["enabled"])

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _read_cycle_config(self) -> Dict[str, Any]:
        """Read enabled/interval from config for one loop cycle.

        FAIL-CLOSED (the opposite direction from
        HNSWOrphanRepairSweepScheduler's fail-open default): a config-read
        glitch must never cause an unintended migration to start deleting
        real on-disk chunk data.
        """
        try:
            cfg = self._config_service.get_config().fleet_migration_config
            interval_minutes = int(cfg.tick_interval_minutes)
            # Codex round-6 MEDIUM finding: a zero/negative interval
            # produces wait_seconds<=0 in _loop(), whose bounded-wait
            # loop condition (elapsed < wait_seconds) is then
            # immediately False -- a continuous busy-spin re-submitting
            # migration jobs with zero delay. Validate a sane positive
            # bound, falling back to the default rather than forwarding
            # a value that would defeat the scheduler's own pacing.
            if interval_minutes <= 0:
                logger.warning(
                    "FleetMigrationScheduler: configured "
                    "tick_interval_minutes=%d is non-positive -- "
                    "falling back to the default of %d minutes to "
                    "avoid a continuous busy-spin",
                    interval_minutes,
                    _DEFAULT_TICK_INTERVAL_MINUTES,
                )
                interval_minutes = _DEFAULT_TICK_INTERVAL_MINUTES
            return {
                "enabled": bool(cfg.enabled),
                "interval_minutes": interval_minutes,
            }
        except Exception as exc:
            logger.warning(
                "FleetMigrationScheduler: failed to read config, defaulting "
                "to DISABLED (fail-closed, this scheduler deletes real "
                "on-disk data): %s",
                exc,
            )
            return {
                "enabled": False,
                "interval_minutes": _DEFAULT_TICK_INTERVAL_MINUTES,
            }

    def _loop(self) -> None:
        """Main loop: submit a migration job (if enabled), then wait for
        the configured interval, repeat. Re-reads enabled/interval from
        config each cycle so Web UI changes take effect without a restart."""
        while not self._stop_event.is_set():
            cycle_cfg = self._read_cycle_config()
            enabled = cycle_cfg["enabled"]
            interval_minutes = cycle_cfg["interval_minutes"]

            if enabled:
                try:
                    self.trigger_now()
                except Exception as exc:
                    logger.error(
                        "FleetMigrationScheduler: error submitting migration job: %s",
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
