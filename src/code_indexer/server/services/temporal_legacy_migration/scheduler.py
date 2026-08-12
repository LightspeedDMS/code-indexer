"""Paced, fail-soft scheduler for the explicit legacy relocation gates."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, cast

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from .discovery import discover_candidates
from .locking import RefreshInProgressError, WriteLockHeldError, guarded_by_refresh_lock
from .mover import MigrationResult, migrate_temporal_shards
from code_indexer.storage.temporal_metadata_backend_registry import (
    get_temporal_metadata_backend_factory,
)

logger = logging.getLogger(__name__)

# Bug/Issue #1548 blocker 4: EVERY scheduler-driven migration pass must be
# guarded by the repo's refresh-safe write lock -- there is no unlocked
# fallback path here (unlike the CLI's explicit-admin-action command,
# which documents its own narrower exception). A caller with no real
# RefreshScheduler available must not construct this scheduler at all.

# Bug #1565: "temporal legacy migration: skipping '<alias>' this pass" is
# a by-design, correctly-handled fact (the write lock is held, or a
# refresh is in flight for this alias -- the pass simply moves on to the
# next candidate, per _migrate_one_candidate's own docstring) that used to
# re-fire on EVERY single pass for as long as the contention stayed
# unresolved -- measured on staging: 122 occurrences in 24h. Bounded to at
# most once per hour PER ALIAS: the first observation of a given alias's
# skip still logs at WARNING, repeat observations within this window are
# demoted to DEBUG, and a fresh WARNING reminder fires once the window
# elapses and the skip is still unresolved. Logging-cadence hint only
# (never correctness state) -- tracked in per-instance RAM.
_SKIP_WARNING_MIN_INTERVAL_SECONDS = 3600.0


class TemporalLegacyMigrationScheduler:
    """Run one filesystem-truth migration pass per configured interval."""

    OPERATION_TYPE = "temporal_legacy_migration"
    _SCHEDULER_REPO_ALIAS = "temporal-legacy-migration-scheduler"

    # Event-driven bounded wait between ticks (not a busy loop -- self._stop
    # is a threading.Event; .wait(N) returns immediately once stop() sets
    # it, and blocks at most N seconds otherwise). Named to avoid an
    # unexplained magic number; matches this codebase's other schedulers.
    _LOOP_TICK_SECONDS = 60

    # Bounded join wait for the scheduler's own daemon thread on stop() --
    # generous enough for an in-flight run_once() pass to notice self._stop
    # and return between ticks, without blocking shutdown indefinitely.
    _STOP_JOIN_TIMEOUT_SECONDS = 10

    def __init__(
        self,
        *,
        # Typed Any deliberately: this scheduler package is intentionally
        # decoupled from the concrete GoldenRepoManager/ConfigService
        # classes (both live in server-startup-heavy modules) -- only
        # list_golden_repos()/get_actual_repo_path() and get_config() are
        # used, and importing the concrete types here would pull in their
        # full dependency graphs for no static-typing benefit over the
        # narrow structural usage below.
        golden_repo_manager: Any,
        config_service: Any,
        # Typed Any deliberately: see locking.guarded_by_refresh_lock's own
        # docstring for why this module never imports RefreshScheduler
        # directly.
        refresh_scheduler: Any,
        background_job_manager: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if refresh_scheduler is None:
            raise ValueError(
                "refresh_scheduler must not be None: every scheduler-driven "
                "migration pass must be guarded by the refresh-safe write "
                "lock (Issue #1548 blocker 4)"
            )
        self._manager = golden_repo_manager
        self._config_service = config_service
        self._background_job_manager = background_job_manager
        self._refresh_scheduler = refresh_scheduler
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.monotonic
        )
        # Bug #1565: per-alias last-WARNING-logged timestamp for the
        # skip-log bounded-cadence throttle. Logging-cadence hint only,
        # never correctness state -- see
        # _SKIP_WARNING_MIN_INTERVAL_SECONDS's own docstring.
        self._skip_warning_last_logged: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> Dict[str, Any]:
        """Run one filesystem-truth migration pass across every candidate.

        Returns a summary dict (stored as the BackgroundJobManager job's
        result -- Blocker 9: this scheduler's own caller previously
        discarded the per-candidate MigrationResult entirely).

        Blocker 4: the config gate is enforced HERE, not only in ``_loop``'s
        pre-check before calling ``trigger_now()`` -- any other direct
        caller of ``run_once()`` (the CLI, a test, a future admin trigger)
        must not be able to bypass operator intent by skipping that outer
        wrapper. When both gates are off, discovery and write-lock
        acquisition are skipped entirely -- there is nothing to migrate.
        """
        settings = self._resolve_settings()
        if not settings.relocation_enabled and not settings.cleanup_authorized:
            logger.debug(
                "temporal legacy migration: both relocation_enabled and "
                "cleanup_authorized are False -- skipping this pass entirely"
            )
            return {**_result_as_dict(MigrationResult()), "per_repo": {}}
        metadata_backend_factory = get_temporal_metadata_backend_factory()

        totals = MigrationResult()
        per_repo: Dict[str, Any] = {}
        for candidate in discover_candidates(self._manager):
            result = self._migrate_one_candidate(
                candidate,
                relocation_enabled=settings.relocation_enabled,
                cleanup_authorized=settings.cleanup_authorized,
                metadata_backend_factory=metadata_backend_factory,
            )
            if result is None:
                continue
            totals = _add_results(totals, result)
            per_repo[candidate.alias] = _result_as_dict(result)
            _log_per_candidate(candidate.alias, result)

        logger.info(
            "temporal legacy migration pass complete: published=%d "
            "already_complete=%d deleted=%d collisions=%d failed=%d",
            totals.published,
            totals.already_complete,
            totals.deleted,
            totals.collisions,
            totals.failed,
        )
        return {**_result_as_dict(totals), "per_repo": per_repo}

    def _resolve_settings(self) -> Any:
        config = self._config_service.get_config()
        settings = config.temporal_legacy_migration_config
        if settings is None:
            raise RuntimeError("temporal_legacy_migration_config is unavailable")
        return settings

    def _log_skip(self, alias: str, exc: Exception) -> None:
        """Bug #1565: log the by-design "skipping this pass" fact at
        WARNING at most once per ``_SKIP_WARNING_MIN_INTERVAL_SECONDS``
        PER ALIAS -- the first observation of a given alias's skip (or the
        first observation once the window has elapsed and the skip is
        STILL unresolved) logs at WARNING; every other repeat observation
        logs at DEBUG. Never affects control flow -- the caller's
        ``return None`` is unconditional either way.
        """
        now = self._clock()
        last_logged = self._skip_warning_last_logged.get(alias)
        message = "temporal legacy migration: skipping %r this pass: %s"
        if (
            last_logged is None
            or (now - last_logged) >= _SKIP_WARNING_MIN_INTERVAL_SECONDS
        ):
            logger.warning(message, alias, exc)
            self._skip_warning_last_logged[alias] = now
        else:
            logger.debug(message, alias, exc)

    def _migrate_one_candidate(
        self,
        # Typed Any deliberately: a TemporalMigrationCandidate from
        # discovery.py, structurally accessed via .alias/.legacy_root/
        # .fixed_root only -- avoiding the import here keeps this module
        # decoupled the same way __init__'s Any parameters are.
        candidate: Any,
        *,
        relocation_enabled: bool,
        cleanup_authorized: bool,
        metadata_backend_factory: Any,
    ) -> Optional[MigrationResult]:
        """Migrate one candidate under the refresh-safe write lock.

        Returns None (never raises) if the lock cannot be acquired or a
        refresh is in flight -- the pass simply moves on to the next
        candidate, logged as a WARNING.
        """
        try:
            with guarded_by_refresh_lock(
                self._refresh_scheduler, candidate.alias
            ) as lock_loss_signal:
                return migrate_temporal_shards(
                    candidate.legacy_root,
                    candidate.fixed_root,
                    relocation_enabled=relocation_enabled,
                    cleanup_authorized=cleanup_authorized,
                    metadata_backend_factory=metadata_backend_factory,
                    lock_lost_check=lock_loss_signal,
                )
        except (WriteLockHeldError, RefreshInProgressError) as exc:
            self._log_skip(candidate.alias, exc)
            return None

    def trigger_now(self) -> Optional[str]:
        """Submit one fleet-wide serialized migration pass."""
        if self._background_job_manager is None:
            raise RuntimeError("temporal migration requires BackgroundJobManager")
        try:
            return cast(
                str,
                self._background_job_manager.submit_job(
                    self.OPERATION_TYPE,
                    self.run_once,
                    submitter_username="system",
                    is_admin=True,
                    repo_alias=self._SCHEDULER_REPO_ALIAS,
                ),
            )
        except DuplicateJobError:
            logger.debug("temporal legacy migration tick already in flight")
            return None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._STOP_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                raise RuntimeError("temporal legacy migration scheduler did not stop")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                settings = self._resolve_settings()
                if settings.relocation_enabled or settings.cleanup_authorized:
                    self.trigger_now()
            except DuplicateJobError:
                logger.debug("temporal legacy migration tick already in flight")
            except Exception:
                logger.exception("temporal legacy migration pass failed")
            self._stop.wait(self._LOOP_TICK_SECONDS)


def _add_results(a: MigrationResult, b: MigrationResult) -> MigrationResult:
    return MigrationResult(
        published=a.published + b.published,
        already_complete=a.already_complete + b.already_complete,
        deleted=a.deleted + b.deleted,
        collisions=a.collisions + b.collisions,
        failed=a.failed + b.failed,
    )


def _result_as_dict(result: MigrationResult) -> Dict[str, int]:
    return {
        "published": result.published,
        "already_complete": result.already_complete,
        "deleted": result.deleted,
        "collisions": result.collisions,
        "failed": result.failed,
    }


def _log_per_candidate(alias: str, result: MigrationResult) -> None:
    logger.info(
        "temporal legacy migration: %r published=%d already_complete=%d "
        "deleted=%d collisions=%d failed=%d",
        alias,
        result.published,
        result.already_complete,
        result.deleted,
        result.collisions,
        result.failed,
    )
    if result.collisions:
        logger.warning(
            "temporal legacy migration: %d collision(s) for %r",
            result.collisions,
            alias,
        )
    if result.failed:
        logger.error(
            "temporal legacy migration: %d shard failure(s) for %r",
            result.failed,
            alias,
        )
