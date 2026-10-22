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
from typing import Any, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

CATALOG_LOCK_ALIAS = "cidx-meta"
# ^ coupled server/mcp/handlers/files.py acquires write mode as owner_name "mcp_write_mode"
_WRITE_MODE_OWNER = "mcp_write_mode"


class CatalogLockedExclusively(Exception):
    """The catalog write lock is held by an owner expecting exclusive access;
    retry once it is released. The message names only a neutral category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(
            f"the cidx-meta catalog is locked for {category}; nothing was "
            f"written -- retry when it is released"
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


def require_publishing_holder(refresh_scheduler: Any) -> None:
    """Return when the current holder of the catalog lock is a cooperative
    catalog writer (a later refresh publishes a write made under it); raise
    CatalogLockedExclusively otherwise, including for a holder that released
    it meanwhile (the caller retries the acquire)."""
    info = refresh_scheduler.write_lock_manager.get_lock_info(CATALOG_LOCK_ALIAS)
    owner: Optional[str] = str(info["owner"]) if info and info.get("owner") else None
    exact, prefixes = _cooperative_owners()
    # ^ hazard a piggyback holds nothing => an exclusive holder may take the lock before the write lands; bounded because writers stage outside the catalog and os.replace (catalog_staged_write), so readers see the old or new file, never a partial one
    if owner is not None and (owner in exact or owner.startswith(prefixes)):
        return
    logger.info(f"cidx-meta write refused: catalog lock held by {owner!r}")
    category = (
        "write mode" if owner == _WRITE_MODE_OWNER else ("another exclusive operation")
    )
    raise CatalogLockedExclusively(category)
