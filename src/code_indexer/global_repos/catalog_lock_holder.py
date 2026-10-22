"""Who may write to cidx-meta while another owner holds its write lock.

A catalog writer that cannot take the cidx-meta write lock may still write
(piggyback) only when the holder is a refresh: the refresh scheduler's
publish lock, whose refresh (or the debouncer's next one, to which the
piggybacked write is owed) picks the write up. Every other holder -- write
mode, a restore, another writer -- expects exclusive access, so the writer
refuses with a retryable error instead of writing or waiting.

The holder is read through the lock manager's ``get_lock_info`` (the file
lock and both DB-backed alias-lock stores answer it with the owner name).
"""

from __future__ import annotations

from typing import Any, Optional

CATALOG_LOCK_ALIAS = "cidx-meta"
#: Owners whose hold of the catalog lock publishes a piggybacked write.
PUBLISHING_OWNERS = frozenset({"refresh_scheduler"})


class CatalogLockedExclusively(Exception):
    """The catalog write lock is held by an owner that will not publish a
    write made under it; retry once it is released."""

    def __init__(self, holder: Optional[str]) -> None:
        self.holder = holder
        super().__init__(
            f"the cidx-meta catalog is locked by {holder or 'an owner that just released it'!r}"
            f" for exclusive use; nothing was written -- retry when it is released"
        )


def require_publishing_holder(refresh_scheduler: Any) -> None:
    """Return when the current holder of the catalog lock will publish a
    write made under it; raise CatalogLockedExclusively otherwise (including
    a holder that released it meanwhile: the caller retries the acquire)."""
    info = refresh_scheduler.write_lock_manager.get_lock_info(CATALOG_LOCK_ALIAS)
    owner = info.get("owner") if info else None
    if owner in PUBLISHING_OWNERS:
        return
    raise CatalogLockedExclusively(str(owner) if owner is not None else None)
