"""Paced, fail-soft scheduler for the explicit legacy relocation gates."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional, cast

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from .discovery import discover_candidates
from .mover import migrate_temporal_shards
from code_indexer.storage.temporal_metadata_backend_registry import (
    get_temporal_metadata_backend_factory,
)

logger = logging.getLogger(__name__)


class TemporalLegacyMigrationScheduler:
    """Run one filesystem-truth migration pass per configured interval."""

    OPERATION_TYPE = "temporal_legacy_migration"
    _SCHEDULER_REPO_ALIAS = "temporal-legacy-migration-scheduler"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        config_service: Any,
        background_job_manager: Optional[Any] = None,
    ) -> None:
        self._manager = golden_repo_manager
        self._config_service = config_service
        self._background_job_manager = background_job_manager
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> None:
        config = self._config_service.get_config()
        settings = config.fleet_migration_config
        if settings is None:
            raise RuntimeError("fleet_migration_config is unavailable")
        metadata_backend_factory = get_temporal_metadata_backend_factory()
        for candidate in discover_candidates(self._manager):
            migrate_temporal_shards(
                candidate.legacy_root,
                candidate.fixed_root,
                relocation_enabled=settings.temporal_legacy_relocation_enabled,
                cleanup_authorized=settings.temporal_legacy_cleanup_authorized,
                metadata_backend_factory=metadata_backend_factory,
            )

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
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("temporal legacy migration scheduler did not stop")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                config = self._config_service.get_config()
                settings = config.fleet_migration_config
                if settings is None:
                    raise RuntimeError("fleet_migration_config is unavailable")
                if (
                    settings.temporal_legacy_relocation_enabled
                    or settings.temporal_legacy_cleanup_authorized
                ):
                    self.trigger_now()
            except DuplicateJobError:
                logger.debug("temporal legacy migration tick already in flight")
            except Exception:
                logger.exception("temporal legacy migration pass failed")
            self._stop.wait(60)
