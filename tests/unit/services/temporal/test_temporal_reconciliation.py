"""Unit tests for shard-aware per-commit temporal reconciliation.

Story #1290 (Epic #1289) AC15/AC16: reconcile_temporal_index() is shard-aware
(commits are grouped into their quarterly shard by timestamp) and uses the
unified "{project}:commit:{hash}:{j}" point_id scheme. A commit is missing
when it is absent from its shard OR present-but-not-durably-completed
(PARTIAL -- stray points deleted so re-indexing does not duplicate/orphan).

These tests drive the real function against a REAL FilesystemVectorStore
(no mocking of the code under test) so the id_index / delete_points contract
is exercised faithfully.
"""

from datetime import datetime, timezone

from code_indexer.services.temporal.models import CommitInfo
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
from code_indexer.services.temporal.temporal_reconciliation import (
    reconcile_temporal_index,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

MODEL_NAME = "voyage-context-4"
SHARD_2024Q1 = "code-indexer-temporal-voyage_context_4-2024Q1"
SHARD_2024Q2 = "code-indexer-temporal-voyage_context_4-2024Q2"

_TS_Q1 = int(datetime(2024, 2, 15, tzinfo=timezone.utc).timestamp())
_TS_Q2 = int(datetime(2024, 5, 15, tzinfo=timezone.utc).timestamp())


def _commit(hash_: str, timestamp: int, message: str = "msg") -> CommitInfo:
    return CommitInfo(
        hash=hash_,
        timestamp=timestamp,
        author_name="A",
        author_email="a@test.com",
        message=message,
        parent_hashes="",
    )


def _write_complete_commit(
    vector_store: FilesystemVectorStore,
    shard_name: str,
    project: str,
    commit_hash: str,
    num_chunks: int = 2,
) -> None:
    """Write points for a commit AND mark it complete (mirrors the real
    indexer's contract: upsert THEN mark_commit_indexed AFTER flush)."""
    if not vector_store.collection_exists(shard_name):
        vector_store.create_collection(shard_name, 8)
    points = [
        {
            "id": f"{project}:commit:{commit_hash}:{j}",
            "vector": [0.1] * 8,
            "payload": {"commit_hash": commit_hash, "chunk_index": j},
        }
        for j in range(num_chunks)
    ]
    vector_store.upsert_points(shard_name, points)
    shard_dir = vector_store.base_path / shard_name
    TemporalProgressiveMetadata(shard_dir).save_completed(commit_hash)


def _write_partial_commit(
    vector_store: FilesystemVectorStore,
    shard_name: str,
    project: str,
    commit_hash: str,
    num_chunks: int = 2,
) -> None:
    """Write points WITHOUT marking complete -- simulates a crash mid-flush."""
    if not vector_store.collection_exists(shard_name):
        vector_store.create_collection(shard_name, 8)
    points = [
        {
            "id": f"{project}:commit:{commit_hash}:{j}",
            "vector": [0.1] * 8,
            "payload": {"commit_hash": commit_hash, "chunk_index": j},
        }
        for j in range(num_chunks)
    ]
    vector_store.upsert_points(shard_name, points)
    # Deliberately NOT calling mark_commit_indexed/save_completed.


class TestMissingCommitsAcrossShards:
    """AC15: reconcile returns exactly the set of missing (absent) commits."""

    def test_commit_absent_from_nonexistent_shard_is_missing(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        all_commits = [_commit("aaa111", _TS_Q1)]

        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["aaa111"]

    def test_complete_commit_is_not_missing(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        all_commits = [_commit("aaa111", _TS_Q1)]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert missing == []

    def test_mixed_shards_only_missing_ones_returned(self, tmp_path):
        """Commits spanning two different quarterly shards: only the
        genuinely missing ones come back, in original chronological order."""
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")
        # bbb222 (Q1) and ccc333 (Q2) are never indexed.

        all_commits = [
            _commit("aaa111", _TS_Q1),
            _commit("bbb222", _TS_Q1),
            _commit("ccc333", _TS_Q2),
        ]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["bbb222", "ccc333"]

    def test_preserves_chronological_order(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit3")

        all_commits = [
            _commit("commit1", _TS_Q1),
            _commit("commit2", _TS_Q1),
            _commit("commit3", _TS_Q1),
            _commit("commit4", _TS_Q1),
        ]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["commit1", "commit2", "commit4"]

    def test_all_commits_indexed_returns_empty(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit1")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "commit2")

        all_commits = [_commit("commit1", _TS_Q1), _commit("commit2", _TS_Q1)]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert missing == []

    def test_no_commits_indexed_returns_all(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        all_commits = [_commit("commit1", _TS_Q1), _commit("commit2", _TS_Q2)]

        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["commit1", "commit2"]


class TestPartialCommitDetectionAndRewrite:
    """AC16: reconcile detects PARTIAL commits (points present, no durable
    completion marker) and deletes their stray points so re-indexing does
    not create duplicates or leave orphaned points behind."""

    def test_partial_commit_is_returned_as_missing(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        all_commits = [_commit("aaa111", _TS_Q1)]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["aaa111"]

    def test_partial_commit_stray_points_are_deleted(self, tmp_path):
        """After reconcile, the partial commit's points are gone from the
        shard -- re-indexing will not create duplicate/orphaned point_ids."""
        from code_indexer.storage.id_index_manager import IDIndexManager

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(
            vector_store, SHARD_2024Q1, "proj", "aaa111", num_chunks=3
        )

        shard_dir = vector_store.base_path / SHARD_2024Q1
        remaining_before = IDIndexManager().rebuild_from_vectors(shard_dir)
        assert len(remaining_before) == 3

        reconcile_temporal_index(vector_store, [_commit("aaa111", _TS_Q1)], MODEL_NAME)

        remaining_after = IDIndexManager().rebuild_from_vectors(shard_dir)
        assert remaining_after == {}, (
            "partial commit's stray points must be deleted, not left as orphans"
        )

    def test_partial_commit_does_not_affect_sibling_complete_commit(self, tmp_path):
        """Deleting a partial commit's points must not disturb another,
        already-complete commit's points in the SAME shard."""
        from code_indexer.storage.id_index_manager import IDIndexManager

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "good111")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "bad222")

        all_commits = [_commit("good111", _TS_Q1), _commit("bad222", _TS_Q1)]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert [c.hash for c in missing] == ["bad222"]

        shard_dir = vector_store.base_path / SHARD_2024Q1
        remaining = IDIndexManager().rebuild_from_vectors(shard_dir)
        assert set(remaining.keys()) == {
            "proj:commit:good111:0",
            "proj:commit:good111:1",
        }

    def test_no_duplicate_points_after_reconcile_then_reindex(self, tmp_path):
        """Simulates the full crash-resume cycle: partial write -> reconcile
        (deletes stray points) -> re-index (fresh complete write) -> exactly
        the expected point count, no duplicates."""
        from code_indexer.storage.id_index_manager import IDIndexManager

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(
            vector_store, SHARD_2024Q1, "proj", "aaa111", num_chunks=2
        )

        reconcile_temporal_index(vector_store, [_commit("aaa111", _TS_Q1)], MODEL_NAME)

        # Re-index (as index_commits() would after reconcile returns it as missing).
        _write_complete_commit(
            vector_store, SHARD_2024Q1, "proj", "aaa111", num_chunks=2
        )

        shard_dir = vector_store.base_path / SHARD_2024Q1
        remaining = IDIndexManager().rebuild_from_vectors(shard_dir)
        assert set(remaining.keys()) == {
            "proj:commit:aaa111:0",
            "proj:commit:aaa111:1",
        }


class TestShardAwareGrouping:
    """Reconcile resolves each commit's shard independently by timestamp."""

    def test_commits_in_different_shards_checked_independently(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_complete_commit(vector_store, SHARD_2024Q1, "proj", "q1_done")
        _write_complete_commit(vector_store, SHARD_2024Q2, "proj", "q2_done")

        all_commits = [
            _commit("q1_done", _TS_Q1),
            _commit("q1_missing", _TS_Q1),
            _commit("q2_done", _TS_Q2),
            _commit("q2_missing", _TS_Q2),
        ]
        missing = reconcile_temporal_index(vector_store, all_commits, MODEL_NAME)

        assert {c.hash for c in missing} == {"q1_missing", "q2_missing"}
