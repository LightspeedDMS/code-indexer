"""repo_has_zero_residual_temporal_dirs() -- Story #1458 AC1 / AC10
completion-gate predicate.

Per AC1's binding Definition of Done and AC10's firing condition: a repo's
migration is complete (and AC10's post-consolidation snapshot may fire)
ONLY once there is ZERO residual in-repo temporal directory of EITHER
shape -- no quarter-shard directory
(``code-indexer-temporal-{slug}-YYYYQN``) and no quarter-less monolith
directory (``code-indexer-temporal-{slug}``) remaining under
``.code-indexer/index/``.

This predicate is deliberately UNCONDITIONAL PHYSICAL ABSENCE -- it does
NOT run a row-existence scan or an ``hnsw_index.bin``-presence check to
re-derive whether a directory "should" be migrated or swept. That
DISPOSITION decision belongs to Story #1457 AC11 / this story's AC1a
(``classify_bootstrap_disposition`` / ``bootstrap_temporal_namespace_to_
sister``). A physically-present directory of either shape FAILS this gate
regardless of its row content -- both a row-bearing "needs bootstrap"
directory and a rowless "empty artifact" directory must end up PHYSICALLY
GONE for this gate to pass.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
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
    written) trivially returns False -- the same "physical absence"
    convention :func:`repo_has_zero_residual_temporal_dirs` uses.
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


def repo_has_zero_residual_temporal_dirs(index_path: Union[str, Path]) -> bool:
    """Return True iff ``index_path`` contains ZERO temporal directories of
    either shape (quarter-shard or quarter-less monolith).

    Args:
        index_path: The repo's ``.code-indexer/index/`` directory.

    Returns:
        True if ``index_path`` does not exist, or exists and contains no
        directory whose name starts with the temporal collection prefix.
        False if any such directory is physically present, regardless of
        whether it holds committed rows.
    """
    index_path = Path(index_path)
    if not index_path.is_dir():
        # No index directory at all -- trivially zero residual dirs.
        return True

    for entry in index_path.iterdir():
        if entry.is_dir() and entry.name.startswith(TEMPORAL_COLLECTION_PREFIX):
            return False

    return True
