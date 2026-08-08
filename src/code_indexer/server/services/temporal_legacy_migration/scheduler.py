"""Paced, fail-soft scheduler for the explicit legacy relocation gates."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .discovery import discover_candidates
from .mover import migrate_temporal_shards

logger = logging.getLogger(__name__)


class TemporalLegacyMigrationScheduler:
    """Run one filesystem-truth migration pass per configured interval."""

    def __init__(self, *, golden_repo_manager: Any, config_service: Any) -> None:
        self._manager = golden_repo_manager
        self._config_service = config_service
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> None:
        config = self._config_service.get_config()
        settings = config.fleet_migration_config
        if settings is None:
            raise RuntimeError("fleet_migration_config is unavailable")
        for candidate in discover_candidates(self._manager):
            migrate_temporal_shards(
                candidate.legacy_root,
                candidate.fixed_root,
                relocation_enabled=settings.temporal_legacy_relocation_enabled,
                cleanup_authorized=settings.temporal_legacy_cleanup_authorized,
            )

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
                self.run_once()
            except Exception:
                logger.exception("temporal legacy migration pass failed")
            self._stop.wait(60)
