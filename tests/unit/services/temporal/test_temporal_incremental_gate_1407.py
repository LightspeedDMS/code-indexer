"""Bug #1407: cheap per-embedder gate (set-difference + disk-derived
staleness) that lets a fully-caught-up temporal refresh skip the ~44-minute
full multi-shard reconcile disk-scan entirely.

Tests drive the REAL FilesystemVectorStore + HNSWIndexManager +
TemporalProgressiveMetadata (no mocking of the code under test), and
literally count vector_*.json reads to prove the "zero chunk reads on the
clean path" perf claim.
"""

from datetime import datetime, timezone

from code_indexer.services.temporal.models import CommitInfo
from code_indexer.services.temporal.temporal_incremental_gate import (
    compute_embedder_indexing_plan,
)
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

EMBEDDER = "voyage-context-4"
SHARD_2024Q1 = "code-indexer-temporal-voyage_context_4-2024Q1"
SHARD_2024Q2 = "code-indexer-temporal-voyage_context_4-2024Q2"
OTHER_EMBEDDER = "embed-v4.0"
SHARD_OTHER_2024Q1 = "code-indexer-temporal-embed_v4_0-2024Q1"

_TS_Q1 = int(datetime(2024, 2, 15, tzinfo=timezone.utc).timestamp())
_TS_Q2 = int(datetime(2024, 5, 15, tzinfo=timezone.utc).timestamp())


def _commit(hash_: str, timestamp: int) -> CommitInfo:
    return CommitInfo(
        hash=hash_,
        timestamp=timestamp,
        author_name="A",
        author_email="a@test.com",
        message="msg",
        parent_hashes="",
    )


def _write_complete_commit(vector_store, shard_name, project, commit_hash):
    if not vector_store.collection_exists(shard_name):
        vector_store.create_collection(shard_name, 8)
    points = [
        {
            "id": f"{project}:commit:{commit_hash}:0",
            "vector": [0.1] * 8,
            "payload": {"commit_hash": commit_hash, "chunk_index": 0},
        }
    ]
    vector_store.upsert_points(shard_name, points)
    shard_dir = vector_store.base_path / shard_name
    TemporalProgressiveMetadata(shard_dir).save_completed(commit_hash)
    # Finalize so is_stale() reads a real fresh metadata flag.
    vector_store.begin_indexing(shard_name)
    vector_store.end_indexing(shard_name)


class TestNoOpTickZeroChunkReads:
    """The core perf claim: a fully caught-up embedder with no stale shards
    triggers ZERO vector_*.json reads."""

    def test_no_new_commits_no_stale_shards_is_empty(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")

        universe = [_commit("commit1", _TS_Q1)]
        plan = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)

        assert plan.is_empty
        assert plan.shard_commits == {}

    def test_no_op_tick_reads_zero_vector_chunk_files(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")
        _write_complete_commit(vector_store, SHARD_2024Q2, "proj", "commit2")

        universe = [_commit("commit1", _TS_Q1), _commit("commit2", _TS_Q2)]

        read_calls = []
        real_open = open

        def spy_open(file, *a, **kw):
            path_str = str(file)
            if "vector_" in path_str and path_str.endswith(".json"):
                read_calls.append(path_str)
            return real_open(file, *a, **kw)

        import builtins

        original_open = builtins.open
        builtins.open = spy_open
        try:
            plan = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)
        finally:
            builtins.open = original_open

        assert plan.is_empty
        assert read_calls == []


class TestNewCommitsCurrentQuarter:
    def test_new_commit_in_current_quarter_bucketed(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")

        universe = [
            _commit("commit1", _TS_Q1),
            _commit("commit2", _TS_Q1),  # new, same quarter
        ]
        plan = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)

        assert not plan.is_empty
        assert set(plan.shard_commits.keys()) == {SHARD_2024Q1}
        assert [c.hash for c in plan.shard_commits[SHARD_2024Q1]] == ["commit2"]


class TestBackdatedCommitReopensOldShard:
    def test_backdated_commit_reopens_old_quarter_shard(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q2, "proj", "commit_recent")

        # A backdated commit lands in Q1, a quarter with NO existing shard yet.
        universe = [
            _commit("commit_recent", _TS_Q2),
            _commit("commit_backdated", _TS_Q1),
        ]
        plan = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)

        assert not plan.is_empty
        assert SHARD_2024Q1 in plan.shard_commits
        assert [c.hash for c in plan.shard_commits[SHARD_2024Q1]] == [
            "commit_backdated"
        ]
        assert SHARD_2024Q2 not in plan.shard_commits


class TestPerEmbedderIsolation:
    def test_lagging_embedder_computed_independently(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")
        # OTHER_EMBEDDER has NOT indexed anything yet.

        universe = [_commit("commit1", _TS_Q1)]

        plan_a = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)
        plan_b = compute_embedder_indexing_plan(vector_store, universe, OTHER_EMBEDDER)

        assert plan_a.is_empty
        assert not plan_b.is_empty
        assert [c.hash for c in plan_b.shard_commits[SHARD_OTHER_2024Q1]] == ["commit1"]


class TestPhysicallyStaleShardZeroNewCommits:
    def test_stale_shard_with_zero_new_commits_still_appears(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")

        collection_path = vector_store._get_collection_path(SHARD_2024Q1)
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        hnsw_manager.mark_stale(collection_path)

        universe = [_commit("commit1", _TS_Q1)]  # already completed, no new work
        plan = compute_embedder_indexing_plan(vector_store, universe, EMBEDDER)

        assert not plan.is_empty
        assert plan.shard_commits[SHARD_2024Q1] == []


class TestSchedulingLimitsAppliedAfterSetDifference:
    def test_max_commits_selects_newest_n_chronologically(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        # Three new commits, no existing shards.
        universe = [
            _commit("c1", _TS_Q1),
            _commit("c2", _TS_Q1 + 100),
            _commit("c3", _TS_Q1 + 200),
        ]

        plan = compute_embedder_indexing_plan(
            vector_store, universe, EMBEDDER, max_commits=2
        )

        assert [c.hash for c in plan.shard_commits[SHARD_2024Q1]] == ["c2", "c3"]

    def test_since_date_filters_new_commits(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        old_ts = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
        universe = [
            _commit("old", old_ts),
            _commit("recent", _TS_Q1),
        ]

        plan = compute_embedder_indexing_plan(
            vector_store, universe, EMBEDDER, since_date="2024-01-01"
        )

        all_hashes = {c.hash for cs in plan.shard_commits.values() for c in cs}
        assert all_hashes == {"recent"}
