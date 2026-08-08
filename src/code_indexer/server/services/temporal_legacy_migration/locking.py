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
from contextlib import contextmanager
from typing import Any, Iterator

from code_indexer.server.repositories.background_jobs import DuplicateJobError

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
) -> Iterator[None]:
    """Acquire the repo's write lock and verify no refresh is in flight.

    Acquires ``refresh_scheduler.write_lock_manager`` immediately, then
    checks ``refresh_scheduler.check_refresh_not_in_progress(bare_alias)``
    BEFORE yielding -- the same order ``run_fleet_migration_for_repo`` uses
    (write lock first, since a refresh registers itself in JobTracker but
    does not hold the write lock, so the lock alone cannot close the
    already-running-refresh TOCTOU gap). The lock is released in ``finally``
    on every path (success, incomplete, or exception).

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
    lock_acquired = refresh_scheduler.write_lock_manager.acquire(
        bare_alias,
        owner_name=MIGRATION_OWNER_NAME,
        ttl_seconds=TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS,
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
        heartbeat_thread = threading.Thread(
            target=_renew_lock_periodically,
            args=(refresh_scheduler.write_lock_manager, bare_alias, stop_heartbeat),
            name=f"temporal-legacy-migration-lock-heartbeat-{bare_alias}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=heartbeat_join_timeout_seconds)
    finally:
        refresh_scheduler.release_write_lock(
            bare_alias, owner_name=MIGRATION_OWNER_NAME
        )
        logger.info("temporal legacy migration: write lock released for %s", bare_alias)


def _renew_lock_periodically(
    write_lock_manager: Any, bare_alias: str, stop_event: threading.Event
) -> None:
    """Background heartbeat: periodically renew *bare_alias*'s lock lease
    for as long as ``stop_event`` is unset, so a migration pass running
    even longer than ``TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS`` never
    depends solely on the TTL alone to keep its lock. Never raises out of
    this thread -- a single missed/failed renewal is logged and retried
    on the next tick, matching this codebase's fail-soft scheduler
    conventions elsewhere.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
        try:
            renewed = write_lock_manager.renew(
                bare_alias,
                owner_name=MIGRATION_OWNER_NAME,
                ttl_seconds=TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS,
            )
            if not renewed:
                logger.warning(
                    "temporal legacy migration: lock renewal for %s did "
                    "not succeed -- lock may no longer be held by this "
                    "process",
                    bare_alias,
                )
        except Exception:
            logger.exception(
                "temporal legacy migration: lock renewal heartbeat failed for %s",
                bare_alias,
            )
