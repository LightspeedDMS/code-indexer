"""Refresh-safe write-lock guard for legacy temporal shard relocation.

Issue #1548 review blocker 4: a live refresh writes a fixed-root temporal
shard IN PLACE (Bug #1529). Without exclusion, this migration package could
copy/verify/delete around a shard while a refresh is actively writing it --
a live production correctness hazard, not a theoretical one. This module
mirrors ``server/services/fleet_migration/orchestrator.py``'s exact usage of
``RefreshScheduler.write_lock_manager`` + ``check_refresh_not_in_progress``
so both packages exclude the same class of race the same way.

Wired into ``scheduler.py``'s ``run_once`` (the live, unattended production
caller) immediately after this module is added.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator


# Bug #1558: this MUST be job_tracker's DuplicateJobError, NOT the
# same-named class in server.repositories.background_jobs. The single
# call this module wraps -- RefreshScheduler.check_refresh_not_in_progress
# (below) -- calls JobTracker.check_operation_conflict() directly, never
# through BackgroundJobManager.submit_job()'s canonical-translation path,
# so it raises job_tracker's class. fleet_migration/orchestrator.py wraps
# the identical call and imports from the same, correct module -- mirror
# that convention here. Importing the wrong (background_jobs) class here
# previously let the raw exception escape the except clause below
# uncaught, surfacing a benign global_repo_refresh collision as a hard
# temporal_legacy_migration job failure instead of the graceful
# RefreshInProgressError every sibling path already produces.
from code_indexer.server.services.job_tracker import DuplicateJobError

logger = logging.getLogger(__name__)

MIGRATION_OWNER_NAME = "temporal-legacy-migration"

# Issue #1548 round-7 fix: Codex reproduced a NORMAL OPERATIONAL bug, not
# an exotic attack -- WriteLockManager.acquire()'s default TTL is 3600s
# (1 hour), but this project's own indexing-path invariant documents that
# migration/indexing work can legitimately run for HOURS with no
# job/subprocess timeout. Once the default TTL elapsed, a second,
# LEGITIMATE acquire (e.g. a concurrent refresh) took ownership WHILE the
# original migration pass was still actively working. Mirrors
# fleet_migration/orchestrator.py's own AC8 fix (MIGRATION_LOCK_TTL_SECONDS
# = 24h) for the identical reason -- reuse the same justified value here
# rather than inventing a shorter one.
TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS = 24 * 60 * 60

# Heartbeat renewal interval: well under the TTL above so a migration that
# runs even LONGER than 24h still holds its lock legitimately, never
# depending solely on a long-but-finite TTL to survive. Module-level (not
# a function default) so tests can monkeypatch it to something fast.
_HEARTBEAT_INTERVAL_SECONDS = 15 * 60


class WriteLockHeldError(RuntimeError):
    """Raised when the repo's write lock is already held by another writer."""


class RefreshInProgressError(RuntimeError):
    """Raised when a refresh job is already active for this alias."""


class LockLostError(RuntimeError):
    """Raised by ``LockLossSignal.raise_if_lost()`` when a background
    heartbeat renewal has failed (or raised) and the lock may no longer be
    held.

    Issue #1548 round-8 fix (Issue 1): previously a renewal failure was
    only logged -- the guarded migration body kept running and kept
    performing destructive filesystem/metadata work regardless. Codex
    reproduced this concretely: forcing three consecutive renewal
    failures still let the destructive migration body run to completion.
    Callers doing destructive work under ``guarded_by_refresh_lock`` MUST
    check the yielded ``LockLossSignal`` immediately before every
    destructive step (see ``mover.py``'s ``_abort_if_lock_lost``) and
    treat this exception exactly like any other hard failure.
    """


class LockLossSignal:
    """Thread-safe flag set by the background heartbeat when a renewal
    attempt fails or raises, signalling that the write lock this signal
    guards may no longer be held.

    Yielded by ``guarded_by_refresh_lock`` so the guarded body can check
    it (via ``is_lost()``/``raise_if_lost()``) immediately before every
    destructive operation, rather than continuing to act on data it may
    no longer have exclusive ownership of.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def mark_lost(self) -> None:
        self._event.set()

    def is_lost(self) -> bool:
        return self._event.is_set()

    def raise_if_lost(self) -> None:
        if self._event.is_set():
            raise LockLostError(
                "write lock renewal failed -- the lock may no longer be "
                "held by this process; aborting before performing further "
                "destructive work"
            )


@contextmanager
def guarded_by_refresh_lock(
    # Typed Any (not RefreshScheduler) deliberately: importing
    # global_repos.refresh_scheduler here would pull the full CLI/global
    # repo stack into this server-only package. Only the write_lock_manager
    # attribute, check_refresh_not_in_progress(), and release_write_lock()
    # methods are used -- callers pass a real RefreshScheduler in production
    # and a lightweight fake exposing that same surface in tests.
    refresh_scheduler: Any,
    bare_alias: str,
) -> Iterator[LockLossSignal]:
    """Acquire the repo's write lock and verify no refresh is in flight.

    Acquires ``refresh_scheduler.write_lock_manager`` immediately, then
    checks ``refresh_scheduler.check_refresh_not_in_progress(bare_alias)``
    BEFORE yielding -- the same order ``run_fleet_migration_for_repo`` uses
    (write lock first, since a refresh registers itself in JobTracker but
    does not hold the write lock, so the lock alone cannot close the
    already-running-refresh TOCTOU gap). The lock is released in ``finally``
    on every path (success, incomplete, or exception).

    Yields a ``LockLossSignal`` (Issue #1548 round-8, Issue 1) that the
    guarded body MUST check immediately before every destructive
    operation -- set by the background heartbeat if a renewal fails or
    raises, since continuing destructive work while the lock may no
    longer be held is exactly the hazard this guard exists to prevent.

    Raises:
        ValueError: refresh_scheduler is None, or bare_alias is not a
            non-blank string.
        WriteLockHeldError: another writer already holds the lock.
        RefreshInProgressError: a refresh job is currently active for this
            alias; the lock is released before raising.
    """
    if refresh_scheduler is None:
        raise ValueError("refresh_scheduler must not be None")
    if not isinstance(bare_alias, str) or not bare_alias.strip():
        raise ValueError("bare_alias must be a non-blank string")

    heartbeat_join_timeout_seconds = 5.0
    # Issue #1548 round-8 fix (Issue 2): a unique per-acquisition token --
    # owner_name alone is shared by every migration pass, so it cannot
    # distinguish THIS acquisition from a later one taken by a different
    # process/pass under the same owner_name after this lock's TTL
    # expired. Passed to every acquire()/renew()/release() call below so
    # WriteLockManager can refuse a renewal/release that is no longer
    # this acquisition's to make.
    owner_token = uuid.uuid4().hex
    lock_acquired = refresh_scheduler.write_lock_manager.acquire(
        bare_alias,
        owner_name=MIGRATION_OWNER_NAME,
        ttl_seconds=TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS,
        owner_token=owner_token,
    )
    if not lock_acquired:
        raise WriteLockHeldError(
            f"write lock for {bare_alias!r} is already held by another writer"
        )
    logger.info("temporal legacy migration: write lock acquired for %s", bare_alias)
    try:
        try:
            refresh_scheduler.check_refresh_not_in_progress(bare_alias)
        except DuplicateJobError as exc:
            raise RefreshInProgressError(
                f"a refresh job ({exc.existing_job_id}) is active for "
                f"{bare_alias!r}; temporal legacy migration did not touch it"
            ) from exc

        # Round-7 fix: the TTL above is generous, but a migration pass is
        # not guaranteed to finish within it -- a genuine heartbeat is the
        # only way to make an arbitrarily-long pass safe. Started only
        # once we're actually about to do work (not during the
        # already-refused early-return paths above), stopped/joined
        # before the lock is released below.
        stop_heartbeat = threading.Event()
        lock_loss_signal = LockLossSignal()
        heartbeat_thread = threading.Thread(
            target=_renew_lock_periodically,
            args=(
                refresh_scheduler.write_lock_manager,
                bare_alias,
                stop_heartbeat,
                owner_token,
                lock_loss_signal,
            ),
            name=f"temporal-legacy-migration-lock-heartbeat-{bare_alias}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield lock_loss_signal
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=heartbeat_join_timeout_seconds)
    finally:
        refresh_scheduler.release_write_lock(
            bare_alias, owner_name=MIGRATION_OWNER_NAME, owner_token=owner_token
        )
        logger.info("temporal legacy migration: write lock released for %s", bare_alias)


def _renew_lock_periodically(
    write_lock_manager: Any,
    bare_alias: str,
    stop_event: threading.Event,
    owner_token: str,
    lock_loss_signal: LockLossSignal,
) -> None:
    """Background heartbeat: periodically renew *bare_alias*'s lock lease
    for as long as ``stop_event`` is unset, so a migration pass running
    even longer than ``TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS`` never
    depends solely on the TTL alone to keep its lock.

    Issue #1548 round-8 fix (Issue 1): a renewal that returns ``False`` or
    raises now marks *lock_loss_signal* -- the guarded body checks this
    before every destructive operation and aborts rather than continuing
    to act on data it may no longer have exclusive ownership of. Never
    raises out of this thread itself.

    Issue #1548 round-8 fix (Issue 3): ``stop_event`` is re-checked
    immediately before each renewal call -- as close together as
    achievable -- to shrink (never fully eliminate, since the renewal
    call itself may still block on I/O) the window between "should this
    heartbeat still be running" and "does it actually attempt a write".
    The matching, load-bearing half of this fix is inside
    ``WriteLockManager._write_renewed_lock_content``, which re-verifies
    the lock's CURRENT on-disk state immediately before its own atomic
    write -- that is what actually closes the race for a renewal already
    blocked past this check.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
        if stop_event.is_set():
            return
        try:
            renewed = write_lock_manager.renew(
                bare_alias,
                owner_name=MIGRATION_OWNER_NAME,
                ttl_seconds=TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS,
                owner_token=owner_token,
            )
            if not renewed:
                logger.error(
                    "temporal legacy migration: lock renewal for %s did "
                    "not succeed -- lock may no longer be held by this "
                    "process; flagging lock as lost so any destructive "
                    "work aborts",
                    bare_alias,
                )
                lock_loss_signal.mark_lost()
        except Exception:
            logger.exception(
                "temporal legacy migration: lock renewal heartbeat failed "
                "for %s -- flagging lock as lost so any destructive work "
                "aborts",
                bare_alias,
            )
            lock_loss_signal.mark_lost()
