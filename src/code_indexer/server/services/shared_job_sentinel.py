"""
Cluster-safe shared-storage sentinel for dep-map re-entrancy protection (Story #1035).

Atomic O_CREAT|O_EXCL lock files on NFS-shared cidx-meta directory prevent
duplicate dep-map analysis jobs across cluster nodes.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "SentinelInfo",
    "ClaimResult",
    "SharedJobSentinel",
    "AnalysisAlreadyRunningError",
]

logger = logging.getLogger(__name__)

# Bug fix (companion to #1058 in v10.91.17): bounded retry for the transient
# empty-file window in read_active() during a concurrent winner's try_claim()
# between os.open(O_CREAT|O_EXCL) and os.write. 3 attempts × 10ms = ≤30ms
# total worst-case latency on the loser code path, only when the race is hit.
# Preserves the Story #1035 O_CREAT|O_EXCL locking primitive.
_READ_ACTIVE_MAX_RETRIES = 3
_READ_ACTIVE_RETRY_DELAY_S = 0.01


class AnalysisAlreadyRunningError(Exception):
    """Raised when a dep-map analysis claim fails because one is already active (Story #1035)."""

    def __init__(self, active_job_id: str) -> None:
        super().__init__(f"Analysis already running: job_id={active_job_id!r}")
        self.active_job_id = active_job_id


@dataclass
class SentinelInfo:
    """Parsed payload from a sentinel lock file."""

    op_type: str
    job_id: str
    node_id: str
    started_at: datetime


@dataclass
class ClaimResult:
    """Result of a SharedJobSentinel.try_claim() call."""

    success: bool
    active: Optional[SentinelInfo]
    replaced_stale: bool = field(default=False)


class SharedJobSentinel:
    """
    Atomic filesystem sentinel for cluster-wide job re-entrancy protection.

    Uses POSIX O_CREAT|O_EXCL semantics — NFSv4-safe on correctly configured mounts.
    Stale sentinels (from crashed nodes) are atomically replaced via tempfile + os.replace.
    """

    def __init__(self, sentinel_dir: Path, stale_timeout_seconds: int) -> None:
        """Initialise sentinel; creates sentinel_dir if missing."""
        self._sentinel_dir = Path(sentinel_dir)
        self._stale_timeout_seconds = stale_timeout_seconds
        self._sentinel_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def try_claim(
        self,
        op_type: str,
        job_id: str,
        node_id: str,
        _retry: bool = False,
    ) -> ClaimResult:
        """
        Atomically claim the sentinel for op_type.

        Returns ClaimResult(success=True) on successful claim.
        Returns ClaimResult(success=False, active=existing) when slot taken by a fresh job.
        Atomically replaces a stale sentinel and returns ClaimResult(success=True, replaced_stale=True).
        """
        sentinel_path = self._sentinel_dir / f"_active_{op_type}.lock"
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "op_type": op_type,
            "job_id": job_id,
            "node_id": node_id,
            "started_at": now_iso,
        }
        payload_bytes = json.dumps(payload).encode()

        try:
            fd = os.open(
                str(sentinel_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
            try:
                os.write(fd, payload_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            info = SentinelInfo(
                op_type=op_type,
                job_id=job_id,
                node_id=node_id,
                started_at=datetime.fromisoformat(now_iso),
            )
            return ClaimResult(success=True, active=info)

        except FileExistsError:
            existing = self.read_active(op_type)
            if existing is None:
                # Race: file disappeared between O_EXCL and read_active — one retry only
                if not _retry:
                    return self.try_claim(op_type, job_id, node_id, _retry=True)
                # Still gone after retry — give up conservatively
                return ClaimResult(success=False, active=None)

            if self.is_stale(existing, self._stale_timeout_seconds):
                self._force_replace(sentinel_path, payload)
                info = SentinelInfo(
                    op_type=op_type,
                    job_id=job_id,
                    node_id=node_id,
                    started_at=datetime.fromisoformat(now_iso),
                )
                return ClaimResult(success=True, active=info, replaced_stale=True)

            return ClaimResult(success=False, active=existing)

    def release(self, op_type: str, expected_job_id: str) -> None:
        """
        Delete the sentinel only when its job_id matches expected_job_id.

        Logs a WARNING and returns without deleting if the sentinel belongs to a
        different job (owner-only safety, AC10).
        """
        existing = self.read_active(op_type)
        if existing is None:
            logger.warning(
                "SharedJobSentinel.release: no sentinel for op_type=%r, nothing to release",
                op_type,
            )
            return

        if existing.job_id != expected_job_id:
            logger.warning(
                "SharedJobSentinel.release: sentinel owned by job_id=%r, "
                "NOT releasing for expected_job_id=%r",
                existing.job_id,
                expected_job_id,
            )
            return

        sentinel_path = self._sentinel_dir / f"_active_{op_type}.lock"
        try:
            os.unlink(str(sentinel_path))
        except FileNotFoundError:
            logger.warning(
                "SharedJobSentinel.release: sentinel file already gone for op_type=%r",
                op_type,
            )

    def read_active(self, op_type: str) -> Optional[SentinelInfo]:
        """Return SentinelInfo for op_type, or None if absent or persistently corrupt.

        Bounded retry on transient empty/corrupt content tolerates the narrow
        window in a concurrent winner's try_claim() between os.open(O_CREAT|O_EXCL)
        — which makes the file visible with 0 bytes — and the subsequent
        os.write that populates it. Without retry, a loser thread that races into
        this window reads an empty file, json.loads("") raises, and the loser
        gets back None for the winner's identity. The locking primitive
        (Story #1035 O_CREAT|O_EXCL invariant) is unchanged.
        """
        sentinel_path = self._sentinel_dir / f"_active_{op_type}.lock"
        last_exc: Optional[Exception] = None
        for attempt in range(_READ_ACTIVE_MAX_RETRIES):
            try:
                raw = sentinel_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            try:
                data = json.loads(raw)
                return SentinelInfo(
                    op_type=data["op_type"],
                    job_id=data["job_id"],
                    node_id=data["node_id"],
                    started_at=datetime.fromisoformat(data["started_at"]),
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                last_exc = exc
                if attempt < _READ_ACTIVE_MAX_RETRIES - 1:
                    time.sleep(_READ_ACTIVE_RETRY_DELAY_S)
                    continue
        logger.warning(
            "SharedJobSentinel.read_active: corrupt sentinel for op_type=%r after "
            "%d retries, treating as absent: %s",
            op_type,
            _READ_ACTIVE_MAX_RETRIES,
            last_exc,
        )
        return None

    def is_stale(self, info: SentinelInfo, timeout_seconds: int) -> bool:
        """Return True when (now - info.started_at) > timeout_seconds."""
        now = datetime.now(timezone.utc)
        started = info.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (now - started).total_seconds()
        return age > timeout_seconds

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _force_replace(self, sentinel_path: Path, payload: dict) -> None:
        """Atomically overwrite sentinel_path with payload via tempfile + os.replace."""
        tmp_name = str(sentinel_path) + f".tmp.{uuid.uuid4().hex}"
        try:
            fd = os.open(tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, json.dumps(payload).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_name, str(sentinel_path))
        except Exception:
            # Best-effort cleanup of tmp file on failure
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
