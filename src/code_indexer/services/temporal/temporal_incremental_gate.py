"""Bug #1407: cheap per-embedder indexing-plan gate.

Two ZERO-chunk-read source-of-truth signals replace the expensive
disk-scan reconcile on the clean (fully caught-up) path:

- WHAT is indexed = the completed-commit SET, the union of each shard's
  durable ``temporal_progress.json`` completed_commits (small JSON reads,
  N = #quarters, no vector I/O).
- WHETHER a shard's HNSW is consistent = the ``is_stale`` flag in that
  shard's ``collection_meta.json`` (metadata-only, no filesystem scan).

If the set-difference (universe - indexed_set) is empty AND no shard is
physically stale, there is nothing to do: zero writes, zero vector-chunk
reads -- the ~44-minute -> ~seconds win this issue is about.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import CommitInfo
from .temporal_collection_naming import (
    base_collection_name,
    get_shard_collection_name,
    is_sharded_temporal_collection,
    resolve_temporal_collection_name,
)
from .temporal_progressive_metadata import TemporalProgressiveMetadata


@dataclass
class EmbedderIndexingPlan:
    """Result of compute_embedder_indexing_plan() for ONE embedder.

    shard_commits maps shard_name -> commits needing (re)indexing this run.
    A shard may map to an EMPTY list when it is physically stale but has no
    new commits bucketed into it this run (Amendment 6 healing case) --
    still needs a repair pass.
    """

    shard_commits: Dict[str, List[CommitInfo]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True iff there is nothing to do: no new commits, no stale shards."""
        return not self.shard_commits


def _disk_shards_for_embedder(vector_store, embedder_name: str) -> List[str]:
    """Amendment 6: DISK-derived shard enumeration for one embedder.

    Uses list_collections() (metadata-only, no chunk reads) filtered by
    base+quarter-suffix naming -- never derived from bucketing new_commits,
    so an already-stale shard whose commits all became unreachable is still
    found and repaired.
    """
    base_name = resolve_temporal_collection_name(embedder_name)
    shards = []
    for name in vector_store.list_collections():
        if not is_sharded_temporal_collection(name):
            continue
        if base_collection_name(name) != base_name:
            continue
        shards.append(name)
    return shards


def _load_indexed_set(vector_store, embedder_name: str) -> set:
    """Union of completed_commits across this embedder's on-disk shards."""
    indexed: set = set()
    for shard in _disk_shards_for_embedder(vector_store, embedder_name):
        shard_dir = vector_store.base_path / shard
        indexed |= TemporalProgressiveMetadata(shard_dir).load_completed()
    return indexed


def _find_stale_shards(vector_store, embedder_name: str) -> set:
    """Disk-derived is_stale enumeration (metadata-only)."""
    from ...storage.hnsw_index_manager import HNSWIndexManager

    # vector_dim is irrelevant to is_stale() (flag-only read, no dimension
    # validation), so a placeholder value avoids requiring callers to know
    # the real dimension just to check staleness.
    hnsw_manager = HNSWIndexManager(vector_dim=1, space="cosine")
    stale = set()
    for shard in _disk_shards_for_embedder(vector_store, embedder_name):
        shard_path = vector_store._get_collection_path(shard)
        if hnsw_manager.is_stale(shard_path):
            stale.add(shard)
    return stale


def _parse_since_date_cutoff(since_date: str) -> float:
    """Parse a YYYY-MM-DD date string into a UTC epoch cutoff timestamp."""
    dt = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def bucket_commits_by_shard(
    commits: List[CommitInfo], embedder_name: str
) -> Dict[str, List[CommitInfo]]:
    """Group commits into their quarterly shard collection name for one
    embedder. Shared by compute_embedder_indexing_plan() (automatic gate)
    and temporal_indexer._reconcile_full_scan_with_barrier() (operator
    --reconcile), keeping the bucketing logic in exactly one place.
    """
    buckets: Dict[str, List[CommitInfo]] = defaultdict(list)
    for commit in commits:
        shard = get_shard_collection_name(
            embedder_name, datetime.fromtimestamp(commit.timestamp, tz=timezone.utc)
        )
        buckets[shard].append(commit)
    return dict(buckets)


def compute_embedder_indexing_plan(
    vector_store,
    universe: List[CommitInfo],
    embedder_name: str,
    max_commits: Optional[int] = None,
    since_date: Optional[str] = None,
) -> EmbedderIndexingPlan:
    """Compute the cheap per-embedder indexing plan (Bug #1407 core gate).

    Args:
        vector_store: FilesystemVectorStore instance.
        universe: FULL reachable commit set (chronological order, no
            narrowing) -- see temporal_indexer._get_commit_history().
        embedder_name: The temporal embedder to compute the plan for.
        max_commits: Scheduling limit applied to new_commits AFTER the
            set-difference -- selects the newest N (chronological order
            preserved), leaving the rest for a future unrestricted run.
        since_date: Scheduling limit (YYYY-MM-DD) applied to new_commits
            AFTER the set-difference.

    Returns:
        EmbedderIndexingPlan. plan.is_empty is True iff there is nothing to
        do (zero writes, zero vector-chunk reads).
    """
    indexed_set = _load_indexed_set(vector_store, embedder_name)
    new_commits = [c for c in universe if c.hash not in indexed_set]

    if since_date:
        cutoff = _parse_since_date_cutoff(since_date)
        new_commits = [c for c in new_commits if c.timestamp >= cutoff]

    if max_commits is not None and max_commits > 0:
        new_commits = new_commits[-max_commits:]

    shard_commits: Dict[str, List[CommitInfo]] = dict(
        bucket_commits_by_shard(new_commits, embedder_name)
    )

    for shard in _find_stale_shards(vector_store, embedder_name):
        shard_commits.setdefault(shard, [])

    return EmbedderIndexingPlan(shard_commits=shard_commits)
