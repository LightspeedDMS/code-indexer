"""
Cache Invalidation Service for CIDX cluster nodes.

Implements dual-strategy cache invalidation for per-node HNSW, FTS, and
payload caches:

Strategy 1 (immediate): Alias JSON mtime/content change detection.
  - Stats alias JSON file on each check_invalidation() call.
  - If mtime has changed, reads the JSON and compares target_path.
  - If target_path changed, the cache is stale -> return True.

Strategy 2 (background cleanup): TTL-based expiry.
  - Tracks when the cache was last loaded (via record_cache_load()).
  - If elapsed time exceeds ttl_seconds, the cache is considered stale.

Works for both standalone and cluster mode because alias JSON files are
always written locally on each node (CoW snapshot + atomic swap).
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CacheInvalidationService:
    """
    Dual-strategy cache invalidation for cluster node caches.

    Tracks alias JSON files to detect when a golden repository has been
    refreshed (mtime/target_path change) and enforces a TTL ceiling so
    that even an un-touched alias file will eventually cause a reload.
    """

    def __init__(self, alias_dir: str, ttl_seconds: int = 300):
        """
        Initialize the service.

        Args:
            alias_dir: Directory containing alias JSON files
                       (e.g. ~/.cidx-server/golden-repos/aliases/).
            ttl_seconds: Maximum age (in seconds) before a cache entry is
                         considered stale regardless of file changes.
                         Default is 300 seconds (5 minutes).
        """
        self._alias_dir = Path(alias_dir)
        self._ttl_seconds = ttl_seconds

        # Per-alias tracking state
        self._mtime_cache: Dict[str, float] = {}  # alias -> last known mtime
        self._content_cache: Dict[str, str] = {}  # alias -> last known target_path
        self._last_loaded: Dict[str, float] = {}  # alias -> wall-clock load time

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_invalidation(self, alias: str) -> bool:
        """
        Check whether the cache for *alias* should be invalidated.

        Checks two independent conditions in order:
          1. TTL expiry: if the cache was loaded more than ttl_seconds ago
             -> stale (True).
          2. File change: stats the alias JSON for mtime; if changed, reads
             target_path; if target_path changed -> stale (True).

        Returns True if the cache should be reloaded, False if still fresh.
        If the alias JSON file does not exist, returns True (force reload so
        the caller can discover the alias is gone).
        """
        alias_file = self._alias_dir / f"{alias}.json"

        # --- Strategy 2: TTL expiry ---
        if alias in self._last_loaded:
            elapsed = time.time() - self._last_loaded[alias]
            if elapsed >= self._ttl_seconds:
                logger.debug(
                    f"Cache invalidation (TTL): alias={alias} "
                    f"elapsed={elapsed:.1f}s ttl={self._ttl_seconds}s"
                )
                return True

        # --- Strategy 1: mtime / content change ---
        if not alias_file.exists():
            logger.debug(
                f"Cache invalidation (missing file): alias={alias} file={alias_file}"
            )
            return True

        try:
            current_mtime = os.stat(alias_file).st_mtime
        except OSError as exc:
            logger.warning(
                f"Cannot stat alias file for {alias}: {exc}; treating as stale"
            )
            return True

        previous_mtime = self._mtime_cache.get(alias)
        if previous_mtime is None:
            # First time we see this alias — no baseline yet; not stale.
            # Record mtime so future checks have a baseline.
            self._mtime_cache[alias] = current_mtime
            # Also prime content cache so we can detect target_path changes.
            target = self._read_target_path(alias_file)
            if target is not None:
                self._content_cache[alias] = target
            return False

        if current_mtime == previous_mtime:
            # File not touched; no change.
            return False

        # mtime changed — read target_path from JSON
        current_target = self._read_target_path(alias_file)
        if current_target is None:
            # JSON unreadable — treat as stale so caller can retry
            logger.warning(
                f"Cache invalidation (unreadable JSON): alias={alias}; treating as stale"
            )
            return True

        previous_target = self._content_cache.get(alias)
        if previous_target != current_target:
            logger.info(
                f"Cache invalidation (target_path changed): alias={alias} "
                f"old={previous_target!r} new={current_target!r}"
            )
            return True

        # mtime changed but target_path is the same (e.g. last_refresh bump only).
        # Update our mtime baseline so we don't re-read the file every call.
        self._mtime_cache[alias] = current_mtime
        return False

    def record_cache_load(self, alias: str, target_path: str) -> None:
        """
        Record that the cache for *alias* was just (re-)loaded.

        Must be called immediately after the caller loads/reloads the cache
        so that both TTL and mtime baselines are reset correctly.

        Args:
            alias: Alias name (e.g. "my-repo-global").
            target_path: The target_path value that was used for this load.
        """
        alias_file = self._alias_dir / f"{alias}.json"
        self._last_loaded[alias] = time.time()
        self._content_cache[alias] = target_path

        # Refresh mtime baseline
        try:
            self._mtime_cache[alias] = os.stat(alias_file).st_mtime
        except OSError:
            # File may have been removed between load and record — that is
            # acceptable; next check_invalidation will detect the absence.
            pass

    def get_current_target_path(self, alias: str) -> Optional[str]:
        """
        Read the current target_path from the alias JSON file on disk.

        Does NOT consult any internal cache — always reads from filesystem.

        Args:
            alias: Alias name.

        Returns:
            target_path string, or None if the alias file is missing or unreadable.
        """
        alias_file = self._alias_dir / f"{alias}.json"
        return self._read_target_path(alias_file)

    def invalidate_all(self) -> List[str]:
        """
        Force-invalidate all tracked aliases.

        Clears all internal tracking state. The next call to
        check_invalidation() for any alias will behave as if it has never
        been seen before (returns False on first sight, then tracks from
        that point on).

        Returns:
            List of alias names that were invalidated.
        """
        invalidated = list(self._mtime_cache.keys())
        self._mtime_cache.clear()
        self._content_cache.clear()
        self._last_loaded.clear()
        logger.info(f"Force-invalidated {len(invalidated)} cached aliases")
        return invalidated

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_target_path(self, alias_file: Path) -> Optional[str]:
        """Read target_path from an alias JSON file. Returns None on any error."""
        try:
            with open(alias_file, "r") as fh:
                data = json.load(fh)
            result: str = data.get("target_path", "")
            return result if result else None
        except (json.JSONDecodeError, IOError, OSError) as exc:
            logger.warning(f"Failed to read alias file {alias_file}: {exc}")
            return None
