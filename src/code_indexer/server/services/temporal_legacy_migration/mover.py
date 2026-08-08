"""Crash-safe per-shard relocation for legacy temporal indexes.

Issue #1548 review blockers 1/2/3/7/8/9. Locked collision policy: "new
shard wins". If the fixed-root shard ALREADY holds real, verified data --
whether that data matches or diverges from the legacy source -- the
fixed-root copy is authoritative and is NEVER overwritten, and its legacy
counterpart is ALSO left untouched (never deleted) in this same pass. The
divergence-or-coincidental-match is counted as a collision and resolution
is deferred to a later, separate manual/cleanup pass.

The one exception: if the fixed-root data was itself placed there by THIS
migration mechanism in a prior pass, it is legitimately "already
complete" -- a genuine multi-pass migration (relocate now, clean up
later) must still be able to converge. Proof of that provenance is a
CONTENT-BOUND digest marker (see ``_PROVENANCE_MARKER_NAME`` below), never
a bare sentinel file: a sentinel alone could be coincidentally or
accidentally present at the target path without this migration having put
it there, which would falsely authorize treating foreign data as "our own
prior work". A legacy shard is deleted ONLY after its own migrated copy
has been read back and field-for-field verified identical to the legacy
source -- "something exists at the target path" is never sufficient.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol

from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)

from .verification import manifest_digest, verify_shard_copy

logger = logging.getLogger(__name__)

_SHARD_PREFIX = "code-indexer-temporal-"
_STAGING_INFIX = ".staging-"

# Blocker 1: written into a shard's staging directory (and therefore into
# the atomically-published target) ONLY by this module's own _publish(),
# holding the SOURCE'S manifest digest at publish time (see
# verification.manifest_digest). A shard is only ever treated as
# "already_complete" on a later pass when: the marker is present, AND its
# recorded digest equals the CURRENT legacy source's digest, AND it equals
# the target's own current digest -- i.e. content-bound proof that neither
# side has changed since a verified publish, not merely "a file with this
# name happens to exist". This is what makes the marker unforgeable by an
# unrelated process that merely creates a same-named file.
#
# Safe by construction against verify_shard_copy()/manifest_digest(): their
# manifest builders (verification.py's _json_manifest/_chunks_manifest)
# enumerate ONLY "vector_*.json" files and a "chunks.db" SQLite store --
# never a generic directory listing/diff -- so this marker file is
# structurally invisible to every manifest computation and can never be
# mistaken for a data record or corrupt a digest.
_PROVENANCE_MARKER_NAME = ".legacy-migration-provenance"


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

    def count_entries(self) -> int: ...


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


def _read_marker_digest(marker_path: Path) -> Optional[str]:
    if not marker_path.is_file():
        return None
    try:
        return marker_path.read_text().strip()
    except OSError:
        logger.exception("failed to read provenance marker %s", marker_path)
        return None


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
        verify_shard_copy(source, staging)
        # Blocker 1: the provenance marker records the digest of what was
        # just verified, written only AFTER verification succeeds, and is
        # itself fsync'd as part of the staged tree below -- a crash
        # before this point leaves no marker, so a later pass correctly
        # refuses to trust the (incomplete or unverified) target.
        (staging / _PROVENANCE_MARKER_NAME).write_text(manifest_digest(staging))
        _fsync_tree(staging)
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


def _classify_existing_target(source: Path, target: Path) -> str:
    """Return "already_complete" or "collision" for a target that already
    holds verified data (Blocker 1's "new shard wins" policy).

    "already_complete" requires content-bound proof: a provenance marker
    at *target* whose recorded digest equals BOTH the current legacy
    source's manifest digest AND the target's own current manifest digest
    -- i.e. neither side has changed since a verified publish by THIS
    migration mechanism. Anything short of that (no marker, a stale
    marker, or content that has since diverged on either side) is a
    collision, even when the content happens to match byte-for-byte --
    a coincidental match is not proof of provenance.
    """
    marker_digest = _read_marker_digest(target / _PROVENANCE_MARKER_NAME)
    verified_prior_publish = (
        marker_digest is not None
        and marker_digest == manifest_digest(source)
        and marker_digest == manifest_digest(target)
    )
    if verified_prior_publish:
        return "already_complete"
    logger.warning(
        "temporal shard collision: fixed root %s already holds data that "
        "is not verifiably a prior publish of this migration mechanism "
        "(marker_digest=%s) -- destination wins, both sides left "
        "untouched pending a later, separate cleanup pass",
        target,
        marker_digest,
    )
    return "collision"


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
    if _has_verified_data(target):
        outcome = _classify_existing_target(source, target)
        if outcome == "collision":
            return "collision"
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


def _run_shard_pass(
    shards: List[Path],
    fixed_root: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
) -> Dict[str, int]:
    """Process every shard, isolating per-shard failures (Blocker 8)."""
    counts = {
        "published": 0,
        "already_complete": 0,
        "deleted": 0,
        "collision": 0,
        "failed": 0,
    }
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
    return counts


def _fixed_metadata_scope_has_data(
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
) -> bool:
    """Read-only check: does the fixed-root metadata scope already hold rows.

    Blocker 5: the SOLE basis for authorizing a cleanup_authorized-driven
    deletion of the legacy scope -- a successful ``copy_collection_scope()``
    call earlier in the SAME run is necessary but deliberately NOT treated
    as sufficient on its own (an empty source no-ops without creating a
    destination file), so this same check is always run before deletion
    regardless of whether relocation was just attempted here or happened in
    a prior pass. A failure to read is never treated as "yes, safe to
    delete" -- it returns False, so cleanup is withheld.
    """
    if not fixed_meta_path.is_dir():
        return False
    try:
        return metadata_backend_factory(fixed_meta_path).count_entries() > 0
    except Exception:
        logger.exception(
            "failed to verify fixed-root temporal metadata scope %s before "
            "authorizing legacy cleanup -- refusing to delete",
            fixed_meta_path,
        )
        return False


def _copy_metadata_scope_if_safe(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
    *,
    relocation_enabled: bool,
    any_collision: bool,
) -> bool:
    """Attempt the metadata-scope copy. Returns True iff it was attempted
    and failed (Blocker 6). Skips entirely (returns False, no attempt) when
    relocation is disabled or a shard collision was detected this pass
    (Blocker 2 -- copying would let ``INSERT OR REPLACE`` silently overwrite
    rows belonging to whatever independently produced the collision).
    """
    if not relocation_enabled:
        return False
    if any_collision:
        logger.warning(
            "temporal legacy migration: withholding metadata scope copy "
            "%s -> %s because at least one shard collision was detected "
            "this pass",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    try:
        metadata_backend_factory(legacy_meta_path).copy_collection_scope(
            fixed_meta_path
        )
        logger.info(
            "copied temporal metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    except Exception:
        logger.exception(
            "failed to copy temporal metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return True


def _delete_metadata_scope_if_safe(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
    *,
    cleanup_authorized: bool,
    all_legacy_shards_gone: bool,
) -> bool:
    """Attempt the metadata-scope deletion. Returns True iff it was
    attempted and failed (Blocker 6). Withheld (returns False, no attempt)
    unless cleanup is authorized, every legacy shard is verified gone, AND
    (Blocker 5) a fresh read-only check proves the fixed-root scope
    actually holds the relocated rows -- see
    ``_fixed_metadata_scope_has_data`` for why a same-run copy attempt
    alone is never treated as sufficient proof.
    """
    if not cleanup_authorized or not all_legacy_shards_gone:
        return False
    if not _fixed_metadata_scope_has_data(fixed_meta_path, metadata_backend_factory):
        logger.warning(
            "temporal legacy migration: refusing to delete metadata scope "
            "%s -- %s does not verifiably hold the relocated rows, despite "
            "cleanup_authorized",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    try:
        metadata_backend_factory(legacy_meta_path).delete_collection_scope()
        logger.info("deleted legacy temporal metadata scope %s", legacy_meta_path)
        return False
    except Exception:
        logger.exception(
            "failed to delete temporal metadata scope %s", legacy_meta_path
        )
        return True


def _sync_metadata_scope(
    legacy_root: Path,
    fixed_root: Path,
    metadata_backend_factory: Optional[Callable[[Path], TemporalMetadataScopeBackend]],
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    all_legacy_shards_gone: bool,
    any_collision: bool,
) -> bool:
    """Copy/delete the repo-level shared temporal-metadata bookkeeping scope.

    Blocker 3: the temporal metadata store lives at the SHARED
    bookkeeping-collection path (``LEGACY_TEMPORAL_COLLECTION``, one store
    per repo index root shared across every quarter/embedder shard) -- NOT
    at each shard's own path. This must run ONCE per repo, never once per
    shard.

    Returns True iff a copy or delete operation was attempted and failed
    (Blocker 6) -- the caller folds this into ``MigrationResult.failed``.
    """
    if metadata_backend_factory is None:
        return False
    legacy_meta_path = legacy_root / LEGACY_TEMPORAL_COLLECTION
    if not legacy_meta_path.is_dir():
        return False
    fixed_meta_path = fixed_root / LEGACY_TEMPORAL_COLLECTION

    copy_failed = _copy_metadata_scope_if_safe(
        legacy_meta_path,
        fixed_meta_path,
        metadata_backend_factory,
        relocation_enabled=relocation_enabled,
        any_collision=any_collision,
    )
    if copy_failed:
        return True

    return _delete_metadata_scope_if_safe(
        legacy_meta_path,
        fixed_meta_path,
        metadata_backend_factory,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
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

    Note: this function performs filesystem mutation with no locking of
    its own -- every caller (scheduler, CLI) MUST wrap it in the repo's
    refresh-safe write lock (``locking.guarded_by_refresh_lock``) so it is
    never invoked concurrently with a live refresh writing the same
    fixed-root shard in place.
    """
    if not legacy_root.is_dir():
        return MigrationResult()

    _cleanup_orphaned_staging_dirs(fixed_root)
    shards = _discover_shards(legacy_root)
    counts = _run_shard_pass(
        shards,
        fixed_root,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        pre_publish_hook=pre_publish_hook,
    )

    # Blocker 5: an empty shard list must never vacuously satisfy "all
    # legacy shards gone" -- a repo with literally zero temporal shards has
    # nothing to have relocated, so its metadata scope (if any) must never
    # be treated as "cleanup complete" on that basis alone.
    all_legacy_shards_gone = bool(shards) and all(
        not shard.exists() for shard in shards
    )
    metadata_failed = _sync_metadata_scope(
        legacy_root,
        fixed_root,
        metadata_backend_factory,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
        any_collision=counts["collision"] > 0,
    )
    if metadata_failed:
        counts["failed"] += 1

    return MigrationResult(
        published=counts["published"],
        already_complete=counts["already_complete"],
        deleted=counts["deleted"],
        collisions=counts["collision"],
        failed=counts["failed"],
    )
