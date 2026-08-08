"""Crash-safe per-shard relocation for legacy temporal indexes.

Issue #1548 review blockers 1/2/3/7/8/9. Locked collision policy: a
fixed-root shard that already holds real, verified data is treated as
complete and left UNTOUCHED -- and its legacy counterpart is ALSO left
untouched (never deleted), the divergence is counted as a collision, and
resolution is deferred to a later manual/cleanup pass. A legacy shard is
deleted ONLY after its own migrated copy has been read back and
field-for-field verified identical to the legacy source -- "something
exists at the target path" is never sufficient.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol

from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)

from .verification import VerificationError, verify_shard_copy

logger = logging.getLogger(__name__)

_SHARD_PREFIX = "code-indexer-temporal-"
_STAGING_INFIX = ".staging-"


class TemporalMetadataScopeBackend(Protocol):
    """Minimal surface this module needs from a temporal metadata backend.

    Both `TemporalMetadataSqliteBackend` and `TemporalMetadataPostgresBackend`
    satisfy this structurally -- typed here (rather than importing either
    concrete class, which would pull psycopg/fastapi into this module) so
    ``metadata_backend_factory``'s return value gets real type checking
    instead of a bare ``object`` + ``# type: ignore``.
    """

    def copy_collection_scope(self, target_collection_path: Path) -> None: ...

    def delete_collection_scope(self) -> None: ...


@dataclass(frozen=True)
class MigrationResult:
    published: int = 0
    already_complete: int = 0
    deleted: int = 0
    collisions: int = 0
    failed: int = 0


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        elif path.is_dir():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_verified_data(path: Path) -> bool:
    """Return True iff *path* holds real, committed temporal rows.

    Blocker 1 fix: replaces a directory-emptiness check (``any(path.
    iterdir())``, which was True for a directory containing nothing but a
    stray ``collection_meta.json``) with this codebase's designated
    layout-aware data-existence primitive. ``on_error="raise"`` because this
    decision gates destructive cleanup -- "cannot verify" must never be
    silently treated as "no data, safe to proceed" (Bug #1529 finding #5).
    """
    if not path.is_dir():
        return False
    return bool(temporal_shard_has_committed_rows(path, on_error="raise"))


def _cleanup_orphaned_staging_dirs(target_parent: Path) -> None:
    """Remove staging directories orphaned by a crash before publish.

    Blocker 8: a crash between ``shutil.copytree`` and the atomic rename
    into place leaves a ``.{name}.staging-{uuid}`` directory permanently
    orphaned -- nothing previously looked for it on a later run. Safe to
    sweep unconditionally here because the caller (scheduler/CLI) holds the
    repo's write lock for the whole pass, so no other process can be
    concurrently writing a staging directory for this same target_parent.
    Any failure (listing the directory, or removing one entry) is logged
    (ERROR) and skipped -- never allowed to abort the migration pass.
    """
    if not target_parent.is_dir():
        return
    try:
        entries = list(target_parent.iterdir())
    except OSError:
        logger.exception(
            "failed to list %s while sweeping orphaned staging directories",
            target_parent,
        )
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.startswith(".") or _STAGING_INFIX not in entry.name:
            continue
        logger.warning("removing orphaned migration staging directory: %s", entry)
        try:
            shutil.rmtree(entry)
        except OSError:
            logger.exception(
                "failed to remove orphaned migration staging directory: %s", entry
            )


def _publish(
    source: Path,
    target: Path,
    pre_publish_hook: Optional[Callable[[], None]],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}{_STAGING_INFIX}{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging)
        _fsync_tree(staging)
        verify_shard_copy(source, staging)
        if pre_publish_hook is not None:
            pre_publish_hook()
        if target.exists():
            # Caller only reaches _publish() when target has already been
            # confirmed to hold no verified data -- clear any stray/partial
            # artifact (e.g. a bare collection_meta.json) before the rename.
            shutil.rmtree(target)
        staging.rename(target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        logger.info("published legacy temporal shard %s -> %s", source, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _process_one_shard(
    source: Path,
    target: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
) -> str:
    """Migrate/verify/cleanup one shard. Returns one of: "published",
    "already_complete", "collision", "skipped", "deleted" (deleted implies
    one of the first two also happened, tracked by the caller).
    """
    target_has_data = _has_verified_data(target)
    if target_has_data:
        try:
            verify_shard_copy(source, target)
        except VerificationError:
            logger.warning(
                "temporal shard collision: fixed root %s already holds "
                "data that diverges from legacy source %s; both sides "
                "left untouched pending manual review",
                target,
                source,
            )
            return "collision"
        outcome = "already_complete"
    elif relocation_enabled:
        _publish(source, target, pre_publish_hook)
        outcome = "published"
    else:
        return "skipped"

    if cleanup_authorized:
        # Blocker 1: re-verify field-for-field equivalence immediately
        # before destroying the legacy copy -- never trust the branch
        # above alone.
        verify_shard_copy(source, target)
        shutil.rmtree(source)
        logger.info("deleted verified legacy temporal shard %s", source)
    return outcome


def _sync_metadata_scope(
    legacy_root: Path,
    fixed_root: Path,
    metadata_backend_factory: Optional[Callable[[Path], TemporalMetadataScopeBackend]],
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    all_legacy_shards_gone: bool,
) -> None:
    """Copy/delete the repo-level shared temporal-metadata bookkeeping scope.

    Blocker 3 fix: the temporal metadata store lives at the SHARED
    bookkeeping-collection path (``LEGACY_TEMPORAL_COLLECTION``, one store
    per repo index root shared across every quarter/embedder shard) -- NOT
    at each shard's own path. This must run ONCE per repo, never once per
    shard: calling ``delete_collection_scope()`` while any sibling shard of
    the same repo has not yet been verified-migrated would destroy metadata
    rows that sibling still needs.
    """
    if metadata_backend_factory is None:
        return
    legacy_meta_path = legacy_root / LEGACY_TEMPORAL_COLLECTION
    if not legacy_meta_path.is_dir():
        return
    fixed_meta_path = fixed_root / LEGACY_TEMPORAL_COLLECTION

    copy_failed = False
    if relocation_enabled:
        try:
            metadata_backend_factory(legacy_meta_path).copy_collection_scope(
                fixed_meta_path
            )
            logger.info(
                "copied temporal metadata scope %s -> %s",
                legacy_meta_path,
                fixed_meta_path,
            )
        except Exception:
            copy_failed = True
            logger.exception(
                "failed to copy temporal metadata scope %s -> %s",
                legacy_meta_path,
                fixed_meta_path,
            )

    if cleanup_authorized and all_legacy_shards_gone and not copy_failed:
        try:
            metadata_backend_factory(legacy_meta_path).delete_collection_scope()
            logger.info("deleted legacy temporal metadata scope %s", legacy_meta_path)
        except Exception:
            logger.exception(
                "failed to delete temporal metadata scope %s", legacy_meta_path
            )


def _discover_shards(legacy_root: Path) -> List[Path]:
    return sorted(
        path
        for path in legacy_root.iterdir()
        if path.name.startswith(_SHARD_PREFIX) and path.is_dir()
    )


def migrate_temporal_shards(
    legacy_root: Path,
    fixed_root: Path,
    *,
    relocation_enabled: bool = False,
    cleanup_authorized: bool = False,
    metadata_backend_factory: Optional[
        Callable[[Path], TemporalMetadataScopeBackend]
    ] = None,
    pre_publish_hook: Optional[Callable[[], None]] = None,
) -> MigrationResult:
    """Relocate every legacy shard for one repo without destroying data
    that has not been positively verified as safely migrated.

    Args:
        legacy_root: The repo's in-clone ``.code-indexer/index`` directory.
        fixed_root: The repo's fixed server-owned temporal root (Bug #1529).
        relocation_enabled: Copy/publish gate (Web UI config flag).
        cleanup_authorized: Destructive legacy-deletion gate (Web UI config
            flag), independent of ``relocation_enabled``.
        metadata_backend_factory: Optional factory for the shared temporal
            metadata bookkeeping scope (see ``_sync_metadata_scope``).
        pre_publish_hook: Optional callable invoked after staging verification
            but before the atomic rename -- test-only seam for crash/restart
            tests (replaces the removed env-var busy-wait, Blocker 7).

    Per-shard failures are isolated: one bad shard is logged and counted,
    never aborting the rest of the pass (Blocker 8).
    """
    if not legacy_root.is_dir():
        return MigrationResult()

    _cleanup_orphaned_staging_dirs(fixed_root)

    counts = {
        "published": 0,
        "already_complete": 0,
        "deleted": 0,
        "collision": 0,
        "failed": 0,
    }
    shards = _discover_shards(legacy_root)

    for source in shards:
        target = fixed_root / source.name
        try:
            outcome = _process_one_shard(
                source,
                target,
                relocation_enabled=relocation_enabled,
                cleanup_authorized=cleanup_authorized,
                pre_publish_hook=pre_publish_hook,
            )
        except Exception:
            counts["failed"] += 1
            logger.exception("temporal legacy migration failed for shard %s", source)
            continue

        if outcome == "collision":
            counts["collision"] += 1
            continue
        if outcome == "skipped":
            continue
        counts[outcome] += 1
        if not source.exists():
            counts["deleted"] += 1

    all_legacy_shards_gone = all(not shard.exists() for shard in shards)
    _sync_metadata_scope(
        legacy_root,
        fixed_root,
        metadata_backend_factory,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
    )

    return MigrationResult(
        published=counts["published"],
        already_complete=counts["already_complete"],
        deleted=counts["deleted"],
        collisions=counts["collision"],
        failed=counts["failed"],
    )
