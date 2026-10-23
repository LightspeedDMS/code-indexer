"""Catalog changes parked while an exclusive owner holds cidx-meta.

A repository description write or removal that finds the cidx-meta write
lock held by an exclusive owner (write mode, the catalog backup, a
restore) must not touch the catalog, and must not be lost either. It is
parked in ``PENDING_DIR_NAME``, a hidden sibling of the catalog (same
filesystem, outside the catalog tree, so no stage-all or snapshot sees
it), as ONE record per alias: ``<alias>.json`` = {"op": "add"|"remove",
"content": ...}, written atomically (staged, then os.replace), so the
latest change for an alias wins and no two parkers can erase each other.

``apply_pending_catalog_changes`` claims a record with an atomic rename to
a unique name before applying it, so a newer change parked meanwhile stays
for the next apply; a failed apply puts the claim back unless a newer
record exists, and a claim abandoned by a dead applier is put back once
older than STALE_CLAIM_SECONDS. The debouncer calls it before it triggers
an owed refresh, and so does every refresh request a catalog writer makes,
so the description converges once the holder releases.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional, Union

from code_indexer.global_repos.catalog_staged_write import (
    SHARED_CATALOG_FILE_MODE,
    write_catalog_file,
)

logger = logging.getLogger(__name__)

PENDING_DIR_NAME = ".cidx-meta-pending"
#: A claim older than this was left by an applier that died mid-apply.
STALE_CLAIM_SECONDS = 15 * 60
OP_ADD = "add"
OP_REMOVE = "remove"
_CATALOG = "cidx-meta"
_RECORD = ".json"
_CLAIM = ".claim-"


def _pending_dir(golden_repos_dir: Union[str, Path]) -> Path:
    return Path(golden_repos_dir) / PENDING_DIR_NAME


def _record(golden_repos_dir: Union[str, Path], alias: str) -> Path:
    return _pending_dir(golden_repos_dir) / f"{alias}{_RECORD}"


def _park(
    golden_repos_dir: Union[str, Path], alias: str, op: str, content: str
) -> None:
    payload = json.dumps({"op": op, "content": content})
    record = _record(golden_repos_dir, alias)
    if not write_catalog_file(
        record.parent, record, payload, mode=SHARED_CATALOG_FILE_MODE
    ):
        # ^ why False means in place but its directory not fsync'd; still the latest change
        logger.warning(f"Parked catalog change {record.name} is not yet durable")


def defer_description(
    golden_repos_dir: Union[str, Path], alias: str, content: str
) -> None:
    """Park *content* as the description of *alias* (replaces any parked
    change for it)."""
    _park(golden_repos_dir, alias, OP_ADD, content)


def defer_removal(golden_repos_dir: Union[str, Path], alias: str) -> None:
    """Park the removal of *alias*'s description (replaces any parked change
    for it)."""
    _park(golden_repos_dir, alias, OP_REMOVE, "")


def pending_operation(golden_repos_dir: Union[str, Path], alias: str) -> Optional[str]:
    """The parked operation for *alias* (OP_ADD / OP_REMOVE), or None."""
    try:
        record = json.loads(_record(golden_repos_dir, alias).read_text("utf-8"))
    except FileNotFoundError:
        return None
    return str(record["op"])


def discard_pending(golden_repos_dir: Union[str, Path], alias: str) -> None:
    """Drop any parked change for *alias* (a removal that succeeded directly
    supersedes it)."""
    _record(golden_repos_dir, alias).unlink(missing_ok=True)


def _record_of(claim: Path) -> Path:
    return claim.with_name(claim.name.split(_CLAIM)[0])


def _claim(record: Path) -> Optional[Path]:
    """Take *record* for applying; None when another applier took it."""
    claim = record.with_name(f"{record.name}{_CLAIM}{uuid.uuid4().hex}")
    try:
        os.rename(record, claim)
    except FileNotFoundError:
        return None
    os.utime(claim)  # the claim's age starts now
    return claim


def _put_back(claim: Path) -> None:
    """Return *claim* to its record name unless a newer record was parked."""
    try:
        os.link(claim, _record_of(claim))
    except FileExistsError:
        logger.info(f"Dropped {claim.name}: a newer change for it is parked")
    except FileNotFoundError:
        return
    claim.unlink(missing_ok=True)


def _apply(catalog: Path, claim: Path) -> None:
    record = json.loads(claim.read_text("utf-8"))
    target = catalog / f"{_record_of(claim).name[: -len(_RECORD)]}.md"
    if record["op"] == OP_ADD:
        if not write_catalog_file(
            catalog, target, record["content"], mode=SHARED_CATALOG_FILE_MODE
        ):
            logger.warning(f"Parked change for {target.name} applied, not yet durable")
    else:
        target.unlink(missing_ok=True)
    logger.info(f"Applied parked catalog change for {target.name}")


def _live_claims(pending: Path) -> int:
    """Put back stale claims; count the live ones (appliers still working)."""
    live = 0
    now = time.time()
    for claim in [p for p in pending.iterdir() if _CLAIM in p.name]:
        try:
            age = now - claim.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > STALE_CLAIM_SECONDS:
            _put_back(claim)
        else:
            live += 1
    return live


def apply_pending_catalog_changes(refresh_scheduler: Any) -> bool:
    """Apply every parked catalog change under the catalog lock, as the
    lifecycle writer (or under a cooperative holder). Returns True when
    nothing remains parked; False when an exclusive holder still has the
    catalog (nothing applied) or another applier's claim is still live."""
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
    live = _live_claims(pending)
    records: List[Path] = sorted(
        p for p in pending.iterdir() if p.name.endswith(_RECORD)
    )
    if not records:
        return live == 0
    catalog = Path(golden) / _CATALOG
    try:
        with admitted_catalog_write(refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER):
            for record in records:
                claim = _claim(record)
                if claim is None:
                    continue
                try:
                    _apply(catalog, claim)
                except Exception:
                    _put_back(claim)
                    raise
                claim.unlink(missing_ok=True)
    except CatalogLockedExclusively as refused:
        logger.info(f"Parked catalog changes wait: {refused}")
        return False
    return live == 0
