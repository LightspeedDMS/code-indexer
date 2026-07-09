"""
Filesystem-backed storage backends for cluster-safe dep-map cache (Story #1035).

Drop-in interface parity with DependencyMapDashboardCacheBackend (SQLite).
Atomic writes via tempfile + os.replace — NFSv4-safe.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FilesystemDashboardCacheBackend:
    """
    Filesystem backend for dependency map dashboard cache (Story #1035).

    Stores a single JSON file at cache_dir/_dashboard_cache.json.
    All writes are atomic (tempfile + os.replace) — NFSv4-safe.

    Interface is drop-in compatible with DependencyMapDashboardCacheBackend.
    """

    _CACHE_FILENAME = "_dashboard_cache.json"

    def __init__(self, cache_dir: Path) -> None:
        """Initialise backend; creates cache_dir if missing."""
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / self._CACHE_FILENAME

    # ------------------------------------------------------------------
    # Public API — interface parity with DependencyMapDashboardCacheBackend
    # ------------------------------------------------------------------

    def get_cached(self) -> Optional[Dict[str, Any]]:
        """
        Return the cached row as a dict or None if no file or corrupt JSON.

        Returns dict with keys: result_json, computed_at, job_id,
        last_failure_message, last_failure_at — or None.
        """
        try:
            raw = self._cache_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "FilesystemDashboardCacheBackend.get_cached: corrupt JSON, "
                "treating as absent: %s",
                exc,
            )
            return None

        return {
            "result_json": data.get("result_json"),
            "computed_at": data.get("computed_at"),
            "job_id": data.get("job_id"),
            "last_failure_message": data.get("last_failure_message"),
            "last_failure_at": data.get("last_failure_at"),
        }

    def is_fresh(self, ttl_seconds: int) -> bool:
        """
        Return True if a cached result exists and is within ttl_seconds.

        Raises:
            ValueError: If ttl_seconds is negative.
        """
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be non-negative, got {ttl_seconds!r}")

        cached = self.get_cached()
        if cached is None:
            return False
        computed_at_str = cached.get("computed_at")
        if not computed_at_str:
            return False

        try:
            computed_at = datetime.fromisoformat(computed_at_str)
            if computed_at.tzinfo is None:
                computed_at = computed_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
            return age_seconds <= ttl_seconds
        except (ValueError, TypeError) as exc:
            logger.warning(
                "FilesystemDashboardCacheBackend.is_fresh: "
                "failed to parse computed_at=%r: %s",
                computed_at_str,
                exc,
            )
            return False

    def set_result(
        self,
        result_json: str,
        computed_at: Optional[datetime] = None,
    ) -> None:
        """
        Atomically write the cached result, clearing job_id and failure fields.

        Args:
            result_json: JSON string of the computed result.
            computed_at: Optional explicit timestamp; defaults to now(UTC).
        """
        if computed_at is None:
            computed_at = datetime.now(timezone.utc)

        existing = self.get_cached() or {}
        payload = {
            "computed_at": computed_at.isoformat(),
            "job_id": None,
            "result_json": result_json,
            "last_failure_message": None,
            "last_failure_at": None,
        }
        # Preserve other fields that may exist
        _ = existing  # existing fields deliberately not merged — set_result resets all
        self._write_atomic(payload)

    def set_cached(self, result_json: str, job_id: Optional[str] = None) -> None:
        """
        Upsert the cached result, clearing job_id and all failure fields.

        Interface-parity with DependencyMapDashboardCacheBackend.set_cached().
        Called by DependencyMapDashboardJobRunner on job success.

        Args:
            result_json: JSON string of the computed result. Must not be None.
            job_id: Accepted for API compatibility but always stored as None.

        Raises:
            ValueError: If result_json is None.
        """
        if result_json is None:
            raise ValueError("result_json must not be None")
        now = datetime.now(timezone.utc)
        payload = {
            "computed_at": now.isoformat(),
            "job_id": None,
            "result_json": result_json,
            "last_failure_message": None,
            "last_failure_at": None,
        }
        self._write_atomic(payload)

    def clear_job_slot(self) -> None:
        """
        Set job_id to None, preserving all other fields.

        Public interface-parity with DependencyMapDashboardCacheBackend.clear_job_slot().
        No-op when no cache file exists.
        """
        cached = self.get_cached()
        if cached is None:
            return
        updated = dict(cached)
        updated["job_id"] = None
        self._write_atomic(updated)

    def mark_job_failed(self, error_message: str) -> None:
        """
        Record a job failure: clear job_id, set failure fields.

        Preserves existing result_json and computed_at so stale cache survives failure.
        Creates the cache file if it does not exist yet.

        Interface-parity with DependencyMapDashboardCacheBackend.mark_job_failed().

        Args:
            error_message: Human-readable error description. Must not be None.

        Raises:
            ValueError: If error_message is None.
        """
        if error_message is None:
            raise ValueError("error_message must not be None")
        now = datetime.now(timezone.utc).isoformat()

        existing = self.get_cached()
        if existing is None:
            payload: Dict[str, Any] = {
                "computed_at": None,
                "job_id": None,
                "result_json": None,
                "last_failure_message": error_message,
                "last_failure_at": now,
            }
        else:
            payload = dict(existing)
            payload["job_id"] = None
            payload["last_failure_message"] = error_message
            payload["last_failure_at"] = now
        self._write_atomic(payload)

    def claim_job_slot(self, new_job_id: str) -> Optional[str]:
        """
        Claim the job slot if currently empty (CAS via atomic read-modify-write).

        Returns:
            None if the claim succeeded (slot was empty).
            The existing job_id string if the slot was already taken.
        """
        cached = self.get_cached()

        if cached is None:
            # No file yet — create with this job_id
            payload: Dict[str, Any] = {
                "computed_at": None,
                "job_id": new_job_id,
                "result_json": None,
                "last_failure_message": None,
                "last_failure_at": None,
            }
            self._write_atomic(payload)
            return None

        existing_job_id = cached.get("job_id")
        if existing_job_id is not None:
            return str(existing_job_id)

        # Slot empty — claim it
        updated = dict(cached)
        updated["job_id"] = new_job_id
        self._write_atomic(updated)
        return None

    def clear_job_slot_for_retry(self) -> None:
        """
        Clear job_id and failure fields to allow a clean retry.

        Preserves result_json and computed_at so stale cache remains available.
        No-op when no cache file exists.
        """
        cached = self.get_cached()
        if cached is None:
            return

        updated = dict(cached)
        updated["job_id"] = None
        updated["last_failure_message"] = None
        updated["last_failure_at"] = None
        self._write_atomic(updated)

    def get_running_job_id(self, job_tracker: Any = None) -> Optional[str]:
        """
        Return the current job_id if a job is actively running, else None.

        If job_tracker is provided, verifies the job is still alive. A job whose
        status is not in ('running', 'pending', 'queued') is considered a zombie:
        the slot is cleared and None is returned.

        When job_tracker.get_job() raises, the exception is logged as a warning
        and job_id is returned conservatively (tracker unavailable should not
        incorrectly evict a legitimately running job).

        Args:
            job_tracker: Optional object with get_job(job_id) returning an object
                         with a .status attribute, or None.

        Returns:
            job_id string if a live job is running, None otherwise.
        """
        cached = self.get_cached()
        if cached is None:
            return None
        job_id = cached.get("job_id")
        if job_id is None:
            return None

        if job_tracker is None:
            return str(job_id)

        try:
            job = job_tracker.get_job(job_id)
            if job is None or job.status not in ("running", "pending", "queued"):
                self.clear_job_slot()
                return None
        except Exception as exc:
            logger.warning(
                "FilesystemDashboardCacheBackend.get_running_job_id: "
                "job_tracker.get_job(%r) raised, treating job as still running: %s",
                job_id,
                exc,
            )
            return str(job_id)

        return str(job_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_atomic(self, payload: Dict[str, Any]) -> None:
        """Write payload as JSON to cache file atomically via tempfile + os.replace."""
        tmp_path = str(self._cache_file) + f".tmp.{uuid.uuid4().hex}"
        try:
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, json.dumps(payload).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, str(self._cache_file))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _clear_job_slot(self) -> None:
        """Set job_id to None, preserving all other fields."""
        cached = self.get_cached()
        if cached is None:
            return
        updated = dict(cached)
        updated["job_id"] = None
        self._write_atomic(updated)
