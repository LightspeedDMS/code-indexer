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
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List

from .models import CommitInfo
from .temporal_collection_naming import get_shard_collection_name
from .temporal_progressive_metadata import TemporalProgressiveMetadata

logger = logging.getLogger(__name__)


def reconcile_temporal_index(
    vector_store,
    all_commits: List[CommitInfo],
    model_name: str,
) -> List[CommitInfo]:
    """Reconcile git history with indexed commits, shard-aware (AC15/AC16).

    Args:
        vector_store: FilesystemVectorStore instance.
        all_commits: Full list of commits from git history (chronological order).
        model_name: The active temporal embedder name (e.g. "voyage-context-4"),
            used to resolve each commit's quarterly shard collection.

    Returns:
        List of missing-or-partial CommitInfo objects, preserving the
        original chronological order of `all_commits`.
    """
    by_shard: Dict[str, List[CommitInfo]] = {}
    for commit in all_commits:
        shard_name = get_shard_collection_name(
            model_name, datetime.fromtimestamp(commit.timestamp, tz=timezone.utc)
        )
        by_shard.setdefault(shard_name, []).append(commit)

    missing_hashes: set = set()

    for shard_name, shard_commits in by_shard.items():
        if not vector_store.collection_exists(shard_name):
            missing_hashes.update(c.hash for c in shard_commits)
            logger.debug(
                "Reconciliation: shard %s does not exist -- %d commit(s) missing",
                shard_name,
                len(shard_commits),
            )
            continue

        # Story #1290 AC16: scan the ACTUAL vector files on disk rather than
        # trusting id_index.bin, which is only rewritten at end_indexing() --
        # a crash mid-shard leaves new points on disk that the cached binary
        # index does not yet know about.  This is the same crash-resilience
        # rationale the legacy discover_indexed_commits_from_disk() used.
        from ...storage.id_index_manager import IDIndexManager

        shard_dir = vector_store.base_path / shard_name
        point_id_to_path = IDIndexManager().rebuild_from_vectors(shard_dir)

        hashes_with_points: Dict[str, List] = {}
        for point_id, json_path in point_id_to_path.items():
            parts = point_id.split(":")
            if len(parts) == 4 and parts[1] == "commit":
                hashes_with_points.setdefault(parts[2], []).append(json_path)

        completed = TemporalProgressiveMetadata(shard_dir).load_completed()

        partial_paths: List = []
        partial_count = 0
        missing_count = 0
        for commit in shard_commits:
            if commit.hash not in hashes_with_points:
                missing_hashes.add(commit.hash)
                missing_count += 1
            elif commit.hash not in completed:
                # AC16: points present but no durable completion marker --
                # a crash mid-flush. Delete the stray points and re-index.
                partial_paths.extend(hashes_with_points[commit.hash])
                missing_hashes.add(commit.hash)
                partial_count += 1
            # else: points present AND marked complete -- skip (already indexed).

        if partial_paths:
            deleted = 0
            for json_path in partial_paths:
                try:
                    json_path.unlink()
                    deleted += 1
                except OSError as exc:
                    logger.warning(
                        "Reconciliation: failed to delete stray point file %s: %s",
                        json_path,
                        exc,
                    )
            logger.info(
                "Reconciliation: shard %s -- deleted %d stray point(s) from "
                "%d PARTIAL commit(s) for re-indexing",
                shard_name,
                deleted,
                partial_count,
            )

        logger.debug(
            "Reconciliation: shard %s -- %d missing, %d partial, %d complete",
            shard_name,
            missing_count,
            partial_count,
            len(shard_commits) - missing_count - partial_count,
        )

    indexed_count = len(all_commits) - len(missing_hashes)
    logger.info(
        "Reconciliation: %d indexed, %d missing (%d%% complete)",
        indexed_count,
        len(missing_hashes),
        (indexed_count * 100 // len(all_commits)) if all_commits else 100,
    )

    return [c for c in all_commits if c.hash in missing_hashes]
