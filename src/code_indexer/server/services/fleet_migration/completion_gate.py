"""repo_temporal_dirs_fully_consolidated() -- Story #1458 AC1 / AC10
completion-gate predicate (Bug #1528 revision).

Per AC1's binding Definition of Done and AC10's firing condition: a repo's
migration is complete (and AC10's post-consolidation snapshot may fire)
ONLY once every one of its temporal shards has actually been migrated.

Bug #1528 changed WHAT "migrated" looks like on disk. Under the retired
Story #1457 sister-location model a migrated namespace was published to a
DIFFERENT location and its in-repo directory reclaimed, so the gate was
unconditional PHYSICAL ABSENCE of every ``code-indexer-temporal*``
directory. Temporal now migrates IN PLACE through the same
``consolidate_collection_in_place`` engine semantic collections use: the
directory legitimately remains and only its internal chunk layout changes.
The gate is therefore a LAYOUT predicate -- every real temporal shard must
verify as fully consolidated (``verify_collection_fully_migrated``) -- and
physical absence is no longer expected, or required, at all.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    parse_physical_temporal_name,
)
from code_indexer.storage.shared.collection_migration import (
    verify_collection_fully_migrated,
)
from code_indexer.utils.file_locking import nfs_safe_fsync

#: New CRITICAL finding (round 2): "consolidation done" and "snapshot
#: published" (AC10) must be DISTINCT, independently-durable states -- a
#: bare discriminator/temporal-dirs completeness check cannot tell a crash
#: before/during the AC10 snapshot trigger from a genuinely finished pass,
#: which would otherwise leave the snapshot permanently unpublished (the
#: repo reads as "migrated" and discovery never revisits it).
_SNAPSHOT_PUBLISHED_MARKER_FILENAME = ".fleet_migration_snapshot_published"


def repo_has_published_post_consolidation_snapshot(
    index_path: Union[str, Path],
) -> bool:
    """True iff the durable post-consolidation-snapshot marker is present
    for this repo's ``.code-indexer/index/`` directory.

    A missing ``index_path`` (repo never indexed, or the marker was never
    written) trivially returns False -- the same "absent means not done"
    convention :func:`repo_temporal_dirs_fully_consolidated` uses.
    """
    return (Path(index_path) / _SNAPSHOT_PUBLISHED_MARKER_FILENAME).is_file()


def invalidate_post_consolidation_snapshot_marker(
    index_path: Union[str, Path],
) -> None:
    """Codex CRITICAL finding (round 4): durably invalidate a PRIOR
    migration generation's snapshot-published marker.

    Without this, a marker written by an earlier successful pass can be
    mistaken for a LATER generation's completion: e.g. generation A
    succeeds (marker written); new unconsolidated data then appears;
    generation B consolidates it successfully but crashes before firing
    its OWN new snapshot -- the stale marker from A would otherwise make
    ``is_repo_already_migrated()`` report the repo as migrated forever,
    even though B's required snapshot never happened.

    Callers invoke this as soon as new unconsolidated work is detected
    (i.e. right before attempting a fresh migration pass, BEFORE the
    per-repo write lock is even acquired) so that a crash ANYWHERE during
    that pass -- including before its own new snapshot fires -- leaves the
    marker durably absent rather than falsely inherited from a prior
    generation. Idempotent: a no-op if the marker is already absent.
    """
    marker_path = Path(index_path) / _SNAPSHOT_PUBLISHED_MARKER_FILENAME
    try:
        marker_path.unlink()
    except FileNotFoundError:
        return

    dir_fd = os.open(str(index_path), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def mark_post_consolidation_snapshot_published(index_path: Union[str, Path]) -> None:
    """Durably record that AC10's post-consolidation snapshot has been
    published for this repo -- called ONLY after
    ``trigger_post_consolidation_snapshot`` has already returned
    successfully, so a crash mid-trigger (never completing) leaves this
    marker absent and the next migration pass simply retries firing the
    snapshot (idempotent -- publishes a fresh snapshot version).

    Atomic + durable (temp file in the SAME directory, flush+fsync, then
    ``os.replace``, then an ``nfs_safe_fsync`` of the containing directory
    so the rename itself survives a crash/power-loss) -- the same pattern
    :func:`~code_indexer.storage.shared.chunk_layout.write_chunks_db_discriminator`
    uses for its own durable flag write.
    """
    index_path = Path(index_path)
    index_path.mkdir(parents=True, exist_ok=True)
    target_path = index_path / _SNAPSHOT_PUBLISHED_MARKER_FILENAME

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(index_path), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                tmp_f.write("")
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(target_path))
        finally:
            if not fd_owned:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    dir_fd = os.open(str(index_path), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def repo_temporal_dirs_fully_consolidated(index_path: Union[str, Path]) -> bool:
    """Return True iff EVERY real temporal shard directory under
    ``index_path`` has been fully consolidated to the ``chunks.db`` layout.

    Bug #1528: this REPLACES the previous physical-absence predicate
    (``repo_has_zero_residual_temporal_dirs``). That predicate encoded the
    retired Story #1457 sister-location model, where a migrated temporal
    namespace was published ELSEWHERE and its in-repo directory reclaimed,
    so "directory still here" meant "not migrated". Temporal now migrates
    IN PLACE through the same ``consolidate_collection_in_place`` engine
    semantic collections use, so the directory legitimately REMAINS -- what
    changes is its internal layout. Physical absence would therefore be
    permanently unreachable and the AC10 snapshot could never fire again.

    Completeness is delegated to :func:`verify_collection_fully_migrated`
    (discriminator set AND zero legacy sharded files left AND chunks.db
    reopens cleanly) -- never a bare discriminator check, for exactly the
    crash-recovery reason that function documents.

    Skipped, matching ``chunk_migration_cli.enumerate_migration_targets``'s
    established exclusions rather than inventing new ones here:
      * the bare ``code-indexer-temporal`` bookkeeping directory (it anchors
        the shared temporal metadata store and is never a shard);
      * any temporal-prefixed name that does not parse as a real
        ``{slug}[-{quarter}]`` physical shard name.

    Args:
        index_path: The repo's ``.code-indexer/index/`` directory.

    Returns:
        True if ``index_path`` does not exist, or contains no real temporal
        shard, or every real temporal shard verifies as fully consolidated.
        False as soon as one real shard is still (even partially) in the
        legacy sharded layout.
    """
    index_path = Path(index_path)
    if not index_path.is_dir():
        # No index directory at all -- trivially nothing left to consolidate.
        return True

    for entry in sorted(index_path.iterdir()):
        if not entry.is_dir() or not entry.name.startswith(TEMPORAL_COLLECTION_PREFIX):
            continue
        if parse_physical_temporal_name(entry.name) is None:
            continue
        if not (entry / "collection_meta.json").is_file():
            # No metadata file means nothing can flip a chunks_db
            # discriminator here, so this directory can never be
            # consolidated. Two very different cases:
            #   * a genuine ROWLESS "empty artifact" (Story #1458 AC1a) --
            #     skip it, or it would block completion forever now that
            #     in-place migration never deletes directories (under the
            #     retired sister model the bootstrap removed it outright);
            #   * a directory that DOES hold legacy vector_*.json rows but
            #     has no metadata -- an un-migratable anomaly (the same
            #     class chunk_migration_cli reports rather than migrates).
            #     That must FAIL this gate loudly: silently reporting the
            #     repo complete would leave real row data unconsolidated
            #     and publish an AC10 snapshot over it.
            if next(entry.rglob("vector_*.json"), None) is not None:
                return False
            continue
        if not verify_collection_fully_migrated(entry):
            return False

    return True
