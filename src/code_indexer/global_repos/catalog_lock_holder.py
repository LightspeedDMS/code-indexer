"""Who may write to cidx-meta while another owner holds its write lock.

A catalog writer that cannot take the cidx-meta write lock may still write
(piggyback) when the holder is a cooperative catalog writer: the refresh
itself, another memory or X-Ray pattern write, the dependency map, the
description lifecycle writers or the meta-directory updater. Each of them
requests a refresh after releasing, and the piggybacked write is owed to
the debouncer until a refresh covers it. Every other holder -- write mode,
a restore, reconciliation, anything unknown -- expects exclusive access, so
the writer refuses with a retryable error instead of writing or waiting.

The holder is read through the lock manager's ``get_lock_info`` (the file
lock and both DB-backed alias-lock stores answer it with the owner name).
A refusal names only a neutral category: an owner name can carry another
user's username, so the raw name is logged server-side only.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, FrozenSet, Iterator, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

CATALOG_LOCK_ALIAS = "cidx-meta"
# ^ coupled server/mcp/handlers/files.py acquires write mode as owner_name "mcp_write_mode"
_WRITE_MODE_OWNER = "mcp_write_mode"
# ^ invariant never cooperative: while the backup's git writes run no catalog writer piggybacks
CATALOG_BACKUP_LOCK_OWNER = "catalog_backup"
# ^ why a backup push carries no timeout, so the lock must outlive a slow push
CATALOG_BACKUP_LOCK_TTL_SECONDS = 24 * 60 * 60
# ^ why a holder read as None (mid-acquire or just released) is re-checked after yielding the processor; no timed sleep
_ADMISSION_ATTEMPTS = 100


class CatalogLockManager(Protocol):
    """The lock-manager surface the backup lock needs (file lock manager or
    alias-lock coordinator)."""

    def acquire(self, alias: str, owner_name: str, ttl_seconds: int) -> bool: ...

    def release(self, alias: str, owner_name: str) -> bool: ...


@contextmanager
def held_catalog_backup_lock(
    write_lock_manager: CatalogLockManager, alias: str = CATALOG_LOCK_ALIAS
) -> Iterator[bool]:
    """Hold the catalog write lock as the exclusive backup owner for the
    backup's git write sequence; yields False (nothing held) when another
    owner holds it. Released on exit, including after an exception."""
    acquired = write_lock_manager.acquire(
        alias,
        owner_name=CATALOG_BACKUP_LOCK_OWNER,
        ttl_seconds=CATALOG_BACKUP_LOCK_TTL_SECONDS,
    )
    try:
        yield acquired
    finally:
        if acquired and not write_lock_manager.release(
            alias, owner_name=CATALOG_BACKUP_LOCK_OWNER
        ):
            logger.warning(
                f"catalog backup lock release for {alias!r} returned False; "
                f"staleness eviction reclaims it"
            )


class CatalogLockedExclusively(Exception):
    """The catalog write lock is held by an owner expecting exclusive access;
    retry once it is released. The message names only a neutral category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(
            f"the cidx-meta catalog is locked for {category}; nothing was "
            f"written -- retry when it is released"
        )


class CatalogBusy(CatalogLockedExclusively):
    """The catalog lock could be neither taken nor attributed to an owner
    after every bounded attempt (another writer mid-acquire), or a writer
    that must hold it itself (the dependency-map repair) found another
    owner holding it; retryable."""

    def __init__(self) -> None:
        self.category = "busy"
        Exception.__init__(
            self,
            "the cidx-meta catalog is busy; nothing was written -- retry shortly",
        )


def _cooperative_owners() -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    """(exact owner names, owner-name prefixes) of the cooperative writers,
    each defined once next to where its writer builds the name."""
    from code_indexer.global_repos.lifecycle_batch_runner import (
        BATCH_RUNNER_LOCK_OWNER,
        LIFECYCLE_WRITER_LOCK_OWNER,
    )
    from code_indexer.global_repos.meta_directory_updater import (
        META_DIRECTORY_UPDATER_LOCK_OWNER,
    )
    from code_indexer.global_repos.refresh_scheduler import REFRESH_LOCK_OWNER
    from code_indexer.server.services.dependency_map_service import (
        DEPENDENCY_MAP_LOCK_OWNER,
    )
    from code_indexer.server.services.memory_store_service import (
        MEMORY_LOCK_OWNER_PREFIX,
    )
    from code_indexer.server.services.xray_pattern_service import (
        XRAY_PATTERN_LOCK_OWNER_PREFIX,
    )

    exact = frozenset(
        {
            REFRESH_LOCK_OWNER,
            DEPENDENCY_MAP_LOCK_OWNER,
            LIFECYCLE_WRITER_LOCK_OWNER,
            BATCH_RUNNER_LOCK_OWNER,
            META_DIRECTORY_UPDATER_LOCK_OWNER,
        }
    )
    return exact, (MEMORY_LOCK_OWNER_PREFIX, XRAY_PATTERN_LOCK_OWNER_PREFIX)


def _holder_owner(refresh_scheduler: Any) -> Optional[str]:
    """The catalog lock's owner name, or None when no owner can be read (the
    lock is free, or another writer is mid-acquire: its lock file exists
    before its owner is written)."""
    info = refresh_scheduler.write_lock_manager.get_lock_info(CATALOG_LOCK_ALIAS)
    return str(info["owner"]) if info and info.get("owner") else None


def admit_catalog_write(refresh_scheduler: Any, owner_name: str) -> bool:
    """The one admission rule for every catalog writer: True when this
    writer now holds the cidx-meta lock as *owner_name* (it must release
    it); False when it may write under a cooperative holder (a piggyback,
    owed to a later refresh). Raises CatalogLockedExclusively, before
    anything is written, only for an OBSERVED non-cooperative owner; a
    holder that reads as None is re-checked, and one still None after every
    attempt is refused as CatalogBusy (retryable), never written under."""
    exact, prefixes = _cooperative_owners()
    for _attempt in range(_ADMISSION_ATTEMPTS):
        if refresh_scheduler.acquire_write_lock(CATALOG_LOCK_ALIAS, owner_name):
            return True
        owner = _holder_owner(refresh_scheduler)
        if owner is None:
            os.sched_yield()
            continue
        # ^ hazard a piggyback holds nothing => an exclusive holder may take the lock before the write lands; bounded because writers stage outside the catalog and os.replace (catalog_staged_write), so readers see the old or new file, never a partial one
        if owner in exact or owner.startswith(prefixes):
            return False
        logger.info(f"cidx-meta write refused: catalog lock held by {owner!r}")
        raise CatalogLockedExclusively(
            "write mode"
            if owner == _WRITE_MODE_OWNER
            else "another exclusive operation"
        )
    logger.info("cidx-meta write refused: the lock holder never became readable")
    raise CatalogBusy()


@contextmanager
def admitted_catalog_write(refresh_scheduler: Any, owner_name: str) -> Iterator[bool]:
    """admit_catalog_write for the duration of a block; yields whether this
    writer holds the lock, and releases a held lock on exit."""
    acquired = admit_catalog_write(refresh_scheduler, owner_name)
    try:
        yield acquired
    finally:
        if acquired and not refresh_scheduler.release_write_lock(
            CATALOG_LOCK_ALIAS, owner_name
        ):
            logger.warning(
                f"cidx-meta write lock release for {owner_name!r} returned False; "
                f"staleness eviction reclaims it"
            )
