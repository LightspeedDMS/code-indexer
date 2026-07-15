"""Bug #1407 Amendment 4 / per-shard orchestration: reconcile_shard() is the
per-shard primitive extracted from reconcile_temporal_index() so the
automatic was_stale-shard repair path (and the operator --reconcile path)
can both reuse it, scoped to ONE shard.

Stray-delete is fail-CLOSED: any unlink() failure raises
StrayDeleteFailedError and aborts -- HNSWIndexManager.rebuild_from_vectors
has no per-point_id dedupe, so a surviving stray with a duplicate point_id
would become a permanent duplicate HNSW entry (Amendment 4).

Tests drive the REAL FilesystemVectorStore (no mocking of the code under
test).
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.services.temporal.models import CommitInfo
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
from code_indexer.services.temporal.temporal_reconciliation import (
    StrayDeleteFailedError,
    reconcile_shard,
    reconcile_temporal_index,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

MODEL_NAME = "voyage-context-4"
SHARD_2024Q1 = "code-indexer-temporal-voyage_context_4-2024Q1"

_TS_Q1 = int(datetime(2024, 2, 15, tzinfo=timezone.utc).timestamp())


def _commit(hash_: str, timestamp: int = _TS_Q1) -> CommitInfo:
    return CommitInfo(
        hash=hash_,
        timestamp=timestamp,
        author_name="A",
        author_email="a@test.com",
        message="msg",
        parent_hashes="",
    )


def _write_partial_commit(
    vector_store: FilesystemVectorStore,
    shard_name: str,
    project: str,
    commit_hash: str,
    num_chunks: int = 2,
) -> None:
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


class TestReconcileShardBasic:
    """reconcile_shard() must classify missing/partial/complete correctly
    for a SINGLE shard, matching reconcile_temporal_index's per-shard logic."""

    def test_missing_commit_on_nonexistent_shard(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        missing = reconcile_shard(
            vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
        )
        assert [c.hash for c in missing] == ["aaa111"]

    def test_partial_commit_returned_missing_and_strays_deleted(self, tmp_path):
        from code_indexer.storage.id_index_manager import IDIndexManager

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        missing = reconcile_shard(
            vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
        )

        assert [c.hash for c in missing] == ["aaa111"]
        shard_dir = vector_store.base_path / SHARD_2024Q1
        assert IDIndexManager().rebuild_from_vectors(shard_dir) == {}

    def test_complete_commit_not_returned(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")
        shard_dir = vector_store.base_path / SHARD_2024Q1
        TemporalProgressiveMetadata(shard_dir).save_completed("aaa111")

        missing = reconcile_shard(
            vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
        )

        assert missing == []


class TestReconcileShardFailClosed:
    """Amendment 4: a required stray-unlink failure raises
    StrayDeleteFailedError -- never logs-and-continues."""

    def test_unlink_failure_raises_stray_delete_failed_error(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            with pytest.raises(StrayDeleteFailedError):
                reconcile_shard(
                    vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
                )

    def test_full_reconcile_also_fail_closed_on_unlink_error(self, tmp_path):
        """The operator --reconcile path (reconcile_temporal_index) reuses
        reconcile_shard, so it inherits fail-closed too (Amendment 4's
        'also run inside the durable stale barrier' requirement)."""
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            with pytest.raises(StrayDeleteFailedError):
                reconcile_temporal_index(vector_store, [_commit("aaa111")], MODEL_NAME)


class TestReconcileShardDirectoryFsync:
    def test_stray_delete_fsyncs_affected_directory(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        _write_partial_commit(vector_store, SHARD_2024Q1, "proj", "aaa111")

        with patch(
            "code_indexer.services.temporal.temporal_reconciliation.nfs_safe_fsync"
        ) as mock_fsync:
            reconcile_shard(vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME)

        assert mock_fsync.called
