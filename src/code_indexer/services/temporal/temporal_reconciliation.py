"""Temporal reconciliation for crash-resilient per-commit indexing.

Story #1290 (Epic #1289) AC15/AC16: reconciliation is SHARD-AWARE and uses
the unified "{project}:commit:{hash}:{j}" point_id scheme. A commit is
missing when EITHER:
  - it has no points at all in its active-slug quarterly shard, OR
  - it has points but is NOT listed in that shard's durable per-commit
    completion marker (a PARTIAL write -- some-but-not-all points flushed
    before a crash, no marker) -- in this case the stray points are deleted
    so re-indexing does not create duplicates or leave orphaned chunk-index
    points behind.

The legacy per-file-diff reconciliation (discover_indexed_commits_from_disk
scanning ":diff:" point_ids, v1-format file cleanup) has been removed as
part of the hard cut -- there is no v1/legacy artifact concept left to clean
up here; blank-out (temporal_blank_out.py) handles legacy collections
wholesale, before reconcile ever runs.

Bug #1407: reconcile_shard() is the per-shard primitive, extracted so the
automatic "was_stale shard" repair path (temporal_indexer.py's per-shard
finalize barrier) can scope reconciliation to ONE shard instead of paying
the full multi-shard disk-scan cost on every tick. reconcile_temporal_index()
is now a thin wrapper that loops reconcile_shard() over every shard -- used
by the operator's explicit --reconcile (unconditional full scan). Both
callers get the same fail-closed stray-delete behavior (Amendment 4): any
unlink() failure raises StrayDeleteFailedError rather than logging and
continuing, because HNSWIndexManager.rebuild_from_vectors has no
per-point_id dedupe -- a surviving stray with a duplicate point_id would
become a permanent duplicate HNSW entry.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set

from code_indexer.utils.file_locking import nfs_safe_fsync

from .models import CommitInfo
from .temporal_collection_naming import get_shard_collection_name
from .temporal_progressive_metadata import TemporalProgressiveMetadata

logger = logging.getLogger(__name__)


class StrayDeleteFailedError(RuntimeError):
    """Bug #1407 Amendment 4: raised when reconcile_shard() cannot delete a
    required stray point file (points present, no durable completion
    marker). Fail-CLOSED -- the caller must abort this shard's processing
    and leave it flagged stale, never proceed to re-embed/rebuild with a
    surviving stray still on disk (rebuild_from_vectors has no per-point_id
    dedupe, so a stray would become a permanent duplicate HNSW entry).
    """


def _fsync_directory(path: Path) -> None:
    """Fsync a directory so entries removed within it survive a crash."""
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def reconcile_shard(
    vector_store,
    shard_name: str,
    shard_commits: List[CommitInfo],
    model_name: str,
) -> List[CommitInfo]:
    """Reconcile ONE shard against disk (Bug #1407 per-shard primitive).

    Args:
        vector_store: FilesystemVectorStore instance.
        shard_name: The quarterly shard collection name to reconcile.
        shard_commits: Commits that bucket into this shard (chronological).
        model_name: Unused directly here (kept for signature symmetry with
            reconcile_temporal_index's per-embedder scoping); shard_name is
            already resolved.

    Returns:
        Subset of shard_commits that is missing-or-partial, preserving
        original order.

    Raises:
        StrayDeleteFailedError: a PARTIAL commit's stray point file could
            not be deleted (Amendment 4, fail-closed).
    """
    if not shard_commits:
        return []

    if not vector_store.collection_exists(shard_name):
        return list(shard_commits)

    # Story #1290 AC16: scan the ACTUAL vector files on disk rather than
    # trusting id_index.bin, which is only rewritten at end_indexing() --
    # a crash mid-shard leaves new points on disk that the cached binary
    # index does not yet know about.
    from ...storage.id_index_manager import IDIndexManager

    shard_dir = vector_store.base_path / shard_name
    point_id_to_path = IDIndexManager().rebuild_from_vectors(shard_dir)

    hashes_with_points: Dict[str, List] = {}
    for point_id, json_path in point_id_to_path.items():
        parts = point_id.split(":")
        if len(parts) == 4 and parts[1] == "commit":
            hashes_with_points.setdefault(parts[2], []).append(json_path)

    completed = TemporalProgressiveMetadata(shard_dir).load_completed()

    missing: List[CommitInfo] = []
    partial_paths: List = []
    for commit in shard_commits:
        if commit.hash not in hashes_with_points:
            missing.append(commit)
        elif commit.hash not in completed:
            # AC16: points present but no durable completion marker -- a
            # crash mid-flush. Delete the stray points and re-index.
            partial_paths.extend(hashes_with_points[commit.hash])
            missing.append(commit)
        # else: points present AND marked complete -- skip (already indexed).

    if partial_paths:
        touched_dirs: Set[Path] = set()
        for json_path in partial_paths:
            try:
                json_path.unlink()
                touched_dirs.add(json_path.parent)
            except OSError as exc:
                raise StrayDeleteFailedError(
                    f"Reconciliation: failed to delete stray point file "
                    f"{json_path} in shard {shard_name}: {exc}"
                ) from exc
        for touched_dir in touched_dirs:
            _fsync_directory(touched_dir)
        logger.info(
            "Reconciliation: shard %s -- deleted %d stray point(s)",
            shard_name,
            len(partial_paths),
        )

    return missing


def reconcile_temporal_index(
    vector_store,
    all_commits: List[CommitInfo],
    model_name: str,
) -> List[CommitInfo]:
    """Reconcile git history with indexed commits, shard-aware (AC15/AC16).

    Operator-facing full multi-shard disk-scan reconcile: loops
    reconcile_shard() over every shard the commit set touches. This is the
    ONLY path that detects out-of-band vector deletion/corruption for
    already-completed commits (the automatic gate intentionally does not).

    Args:
        vector_store: FilesystemVectorStore instance.
        all_commits: Full list of commits from git history (chronological order).
        model_name: The active temporal embedder name (e.g. "voyage-context-4"),
            used to resolve each commit's quarterly shard collection.

    Returns:
        List of missing-or-partial CommitInfo objects, preserving the
        original chronological order of `all_commits`.

    Raises:
        StrayDeleteFailedError: a PARTIAL commit's stray point file could
            not be deleted in some shard (Amendment 4, fail-closed).
    """
    by_shard: Dict[str, List[CommitInfo]] = {}
    for commit in all_commits:
        shard_name = get_shard_collection_name(
            model_name, datetime.fromtimestamp(commit.timestamp, tz=timezone.utc)
        )
        by_shard.setdefault(shard_name, []).append(commit)

    missing_hashes: set = set()
    for shard_name, shard_commits in by_shard.items():
        shard_missing = reconcile_shard(
            vector_store, shard_name, shard_commits, model_name
        )
        missing_hashes.update(c.hash for c in shard_missing)

    indexed_count = len(all_commits) - len(missing_hashes)
    logger.info(
        "Reconciliation: %d indexed, %d missing (%d%% complete)",
        indexed_count,
        len(missing_hashes),
        (indexed_count * 100 // len(all_commits)) if all_commits else 100,
    )

    return [c for c in all_commits if c.hash in missing_hashes]
