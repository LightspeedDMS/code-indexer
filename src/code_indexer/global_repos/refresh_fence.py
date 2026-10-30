"""Fence a running refresh when it loses ownership of its job or lease.

``RefreshFence`` is the ONE callable the refresh passes as ``cancel_check``
to every step (indexing children via ``run_with_popen_progress``, every
other subprocess via ``run_with_cancel``, and the publish checkpoints).
It answers True ("stop") when the job was cancelled, or when this run is
POSITIVELY no longer the owner:

- job ownership (claimed path only): ``job_owned()`` returned False, i.e.
  the claimed job was re-claimed, reset or cancelled since this run's claim
  generation;
- alias lease: the exact-token lease renewal raised
  ``AliasLockOwnershipLostError`` (probed at most once per
  ``LEASE_PROBE_INTERVAL_SECONDS``; a renewal never shortens the lease).

A read that fails (store unreachable, timeout) never stops the run: the
lease cannot be taken by anyone else while the store is unreachable
either. Fencing is not a timeout: nothing here measures the run's age.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

JOB_OWNERSHIP_LOST = "job_ownership_lost"
LEASE_LOST = "lease_lost"
LEASE_PROBE_INTERVAL_SECONDS = 30.0
_READ_FAILURE_WARN_EVERY = 30


class RefreshOwnershipLostError(RuntimeError):
    """The refresh stopped before its alias swap because another run owns
    its job or lease: nothing was published, and it never self-heals,
    records a strike or schedules an owed retry. On the claimed path the job
    row is left untouched (it belongs to the run that re-claimed it); on the
    BackgroundJobManager path (lease loss) the job is recorded failed on its
    own row."""

    def __init__(self, alias: str, reason: str) -> None:
        self.alias = alias
        self.reason = reason
        super().__init__(
            f"Refresh of {alias} stopped: {reason}; another run owns it now, "
            f"nothing was published"
        )


class RefreshFence:
    def __init__(
        self,
        cancel_check: Optional[Callable[[], bool]],
        job_owned: Optional[Callable[[], bool]],
        *,
        alias: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cancel_check = cancel_check
        self._job_owned = job_owned
        self._alias = alias
        self._clock = clock
        self._lease_probe: Optional[Callable[[], None]] = None
        self._next_probe_at = 0.0
        self._job_read_failures = 0
        self.lost_reason: Optional[str] = None

    def arm_lease_probe(self, probe: Callable[[], None]) -> None:
        """Start probing the lease just acquired (first probe one interval
        from now). ``probe`` raises AliasLockOwnershipLostError on loss."""
        self._lease_probe = probe
        self._next_probe_at = self._clock() + LEASE_PROBE_INTERVAL_SECONDS

    def disarm_lease_probe(self) -> None:
        self._lease_probe = None

    def __call__(self) -> bool:
        if self.lost_reason is not None:
            return True
        # ^ order cancel_check before job_owned; a job_owned answer may reflect that read
        cancelled = self._cancel_check is not None and bool(self._cancel_check())
        if not self._job_still_owned():
            return self._fence(JOB_OWNERSHIP_LOST)
        if cancelled:
            return True
        if self._lease_lost():
            return self._fence(LEASE_LOST)
        return False

    def _fence(self, reason: str) -> bool:
        self.lost_reason = reason
        logger.warning(
            "Refresh of %s fenced (%s): stopping its subprocesses; another run "
            "owns it now",
            self._alias,
            reason,
        )
        return True

    def _job_still_owned(self) -> bool:
        if self._job_owned is None:
            return True
        try:
            owned = bool(self._job_owned())
        except Exception as exc:  # noqa: BLE001 - an unreadable answer is not a loss
            self._job_read_failures += 1
            failures = self._job_read_failures
            if failures == 1 or failures % _READ_FAILURE_WARN_EVERY == 0:
                logger.warning(
                    "Job ownership of the refresh of %s unreadable (consecutive "
                    "failure %d); the run keeps running: %s: %s",
                    self._alias,
                    failures,
                    type(exc).__name__,
                    exc,
                )
            return True
        self._job_read_failures = 0
        return owned

    def _lease_lost(self) -> bool:
        if self._lease_probe is None or self._clock() < self._next_probe_at:
            return False
        self._next_probe_at = self._clock() + LEASE_PROBE_INTERVAL_SECONDS
        from code_indexer.server.services.alias_lock_store.base import (
            AliasLockOwnershipLostError,
        )

        try:
            self._lease_probe()
        except AliasLockOwnershipLostError:
            return True
        except Exception as exc:  # noqa: BLE001 - a store error is not a loss
            logger.warning(
                "Lease probe of the refresh of %s failed; the run keeps "
                "running: %s: %s",
                self._alias,
                type(exc).__name__,
                exc,
            )
        return False
