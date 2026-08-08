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
from contextlib import contextmanager
from typing import Any, Iterator

from code_indexer.server.repositories.background_jobs import DuplicateJobError

logger = logging.getLogger(__name__)

MIGRATION_OWNER_NAME = "temporal-legacy-migration"


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

    lock_acquired = refresh_scheduler.write_lock_manager.acquire(
        bare_alias, owner_name=MIGRATION_OWNER_NAME
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
        yield
    finally:
        refresh_scheduler.release_write_lock(
            bare_alias, owner_name=MIGRATION_OWNER_NAME
        )
        logger.info("temporal legacy migration: write lock released for %s", bare_alias)
