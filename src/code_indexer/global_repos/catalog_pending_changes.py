"""Catalog changes parked while an exclusive owner holds cidx-meta.

A repository description write or removal that finds the cidx-meta write
lock held by an exclusive owner (write mode, the catalog backup, a
restore) must not touch the catalog, and must not be lost either. It is
parked in ``PENDING_DIR_NAME``, a hidden sibling of the catalog (same
filesystem, outside the catalog tree, so no stage-all or snapshot sees
it): ``<alias>.md`` holds a description to put in place, ``<alias>.remove``
marks a description to delete. The latest change per alias wins.

``apply_pending_catalog_changes`` applies every parked change under the
catalog lock (or a cooperative holder); the debouncer calls it before it
triggers an owed refresh, and so does every refresh request a catalog
writer makes, so the description converges once the holder releases.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Union

from code_indexer.global_repos.catalog_staged_write import (
    SHARED_CATALOG_FILE_MODE,
    write_catalog_file,
)

logger = logging.getLogger(__name__)

PENDING_DIR_NAME = ".cidx-meta-pending"
_CATALOG = "cidx-meta"
_DESCRIPTION = ".md"
_REMOVAL = ".remove"


def _pending_dir(golden_repos_dir: Union[str, Path]) -> Path:
    return Path(golden_repos_dir) / PENDING_DIR_NAME


def _park(pending: Path, name: str, content: str, drop: str) -> None:
    """Park *name* (replacing the opposite change *drop* for the alias)."""
    target = pending / name
    if not write_catalog_file(pending, target, content, mode=SHARED_CATALOG_FILE_MODE):
        # ^ why False means in place but its directory not fsync'd; still the latest change
        logger.warning(f"Parked catalog change {name} is not yet durable")
    (pending / drop).unlink(missing_ok=True)


def defer_description(
    golden_repos_dir: Union[str, Path], alias: str, content: str
) -> None:
    """Park *content* as the description of *alias*."""
    _park(
        _pending_dir(golden_repos_dir),
        f"{alias}{_DESCRIPTION}",
        content,
        f"{alias}{_REMOVAL}",
    )


def defer_removal(golden_repos_dir: Union[str, Path], alias: str) -> None:
    """Park the removal of *alias*'s description."""
    _park(
        _pending_dir(golden_repos_dir),
        f"{alias}{_REMOVAL}",
        "",
        f"{alias}{_DESCRIPTION}",
    )


def _consume(entry: Path, seen: bytes) -> None:
    """Delete a parked entry only if it still holds what was applied."""
    # ^ hazard a change parked between this read and unlink is lost; the window is one read
    try:
        if entry.read_bytes() == seen:
            entry.unlink()
    except FileNotFoundError:
        return


def _apply_one(catalog: Path, entry: Path) -> bool:
    """Apply one parked change; True when it was applied durably."""
    seen = entry.read_bytes()
    if entry.name.endswith(_DESCRIPTION):
        durable = write_catalog_file(
            catalog,
            catalog / entry.name,
            seen.decode("utf-8"),
            mode=SHARED_CATALOG_FILE_MODE,
        )
        if not durable:
            logger.warning(f"Parked change {entry.name} applied, not yet durable")
            return False
    else:
        alias = entry.name[: -len(_REMOVAL)]
        (catalog / f"{alias}{_DESCRIPTION}").unlink(missing_ok=True)
    _consume(entry, seen)
    logger.info(f"Applied parked catalog change {entry.name}")
    return True


def apply_pending_catalog_changes(refresh_scheduler: Any) -> bool:
    """Apply every parked catalog change under the catalog lock, as the
    lifecycle writer (or under a cooperative holder). Returns True when
    nothing remains parked; False when an exclusive holder still has the
    catalog (nothing applied) or a change is not yet durable (kept parked;
    the caller retries later)."""
    from code_indexer.global_repos.catalog_lock_holder import (
        CatalogLockedExclusively,
        admitted_catalog_write,
    )
    from code_indexer.global_repos.lifecycle_batch_runner import (
        LIFECYCLE_WRITER_LOCK_OWNER,
    )

    golden = getattr(refresh_scheduler, "golden_repos_dir", None)
    if golden is None:
        # ^ why a scheduler without a golden-repos root has no catalog to park changes for
        return True
    pending = _pending_dir(golden)
    if not pending.is_dir():
        return True
    entries = sorted(
        p for p in pending.iterdir() if p.name.endswith((_DESCRIPTION, _REMOVAL))
    )
    if not entries:
        return True
    catalog = Path(golden) / _CATALOG
    try:
        with admitted_catalog_write(refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER):
            applied = [_apply_one(catalog, entry) for entry in entries]
    except CatalogLockedExclusively as refused:
        logger.info(f"Parked catalog changes wait: {refused}")
        return False
    return all(applied)
