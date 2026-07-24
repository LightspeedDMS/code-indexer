"""Temporal reconciliation rewritten against the consolidated SQLite chunk
store (Story #1457 AC7).

`reconcile_shard` currently uses `IDIndexManager().rebuild_from_vectors`,
which rebuilds the point_id->file_path map from `vector_*.json` files -- a
shape that no longer exists once a shard is consolidated into a CHUNKS_DB
layout. This module tests the NEW CHUNKS_DB-aware branch: point-id
existence is read from `ChunkStore.all_point_ids()` (the consolidated
store's primary key) instead of the retired binary index, WHILE preserving
the transactional-delete + fail-closed + durable-fsync safety contract
(now via `ChunkStore.delete_stray_points_fail_closed`).

The pre-existing legacy SHARDED_JSON branch (tested in
test_temporal_reconciliation_shard_scoped_1407.py) is UNCHANGED and its
tests continue to pass unmodified -- this is a layout-DISPATCHED rewrite,
never a blanket replacement (Story #1456's established dual-mode pattern).

Real `ChunkStore`, real filesystem -- no mocking of the code under test.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_indexer.services.temporal.models import CommitInfo
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
from code_indexer.services.temporal.temporal_reconciliation import (
    StrayDeleteFailedError,
    reconcile_shard,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

MODEL_NAME = "voyage-context-4"
SHARD_2024Q1 = "code-indexer-temporal-voyage_context_4-2024Q1"
VECTOR_DIM = 8

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


def _make_chunks_db_shard(vector_store: FilesystemVectorStore, shard_name: str) -> Path:
    """Build a MINIMAL CHUNKS_DB-layout shard directory at the FLAT
    `vector_store.base_path / shard_name` location -- the same shape
    reconcile_shard's existing legacy path already scans, just with the
    consolidated chunks.db layout instead of hash-sharded vector_*.json
    files. No HNSW build needed -- reconciliation only reads point_ids."""
    shard_dir = vector_store.base_path / shard_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "collection_meta.json").write_text(
        json.dumps({"vector_size": VECTOR_DIM, "vector_dim": VECTOR_DIM})
    )
    store = ChunkStore(shard_dir / "chunks.db", expected_dim=VECTOR_DIM)
    store.close()
    write_chunks_db_discriminator(shard_dir)
    return Path(shard_dir)


def test_missing_commit_detected_in_chunks_db_layout(tmp_path):
    vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
    _make_chunks_db_shard(vector_store, SHARD_2024Q1)

    missing = reconcile_shard(
        vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
    )

    assert [c.hash for c in missing] == ["aaa111"]


def test_completed_commit_with_chunks_db_points_is_not_reported_missing(tmp_path):
    """Genuinely discriminating: a commit with real points in chunks.db AND
    a completion marker must NOT be missing. The OLD
    IDIndexManager().rebuild_from_vectors code scans for vector_*.json
    files -- which do not exist in CHUNKS_DB layout -- so it would find
    ZERO points and incorrectly report even this fully-completed commit as
    missing."""
    vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
    shard_dir = _make_chunks_db_shard(vector_store, SHARD_2024Q1)

    store = ChunkStore(shard_dir / "chunks.db", expected_dim=VECTOR_DIM)
    try:
        store.write_batch(
            [
                {
                    "id": "proj:commit:aaa111:0",
                    "vector": [0.1] * VECTOR_DIM,
                    "payload": {"commit_hash": "aaa111"},
                }
            ]
        )
    finally:
        store.close()
    TemporalProgressiveMetadata(shard_dir).mark_completed(["aaa111"])

    missing = reconcile_shard(
        vector_store, SHARD_2024Q1, [_commit("aaa111")], MODEL_NAME
    )

    assert missing == []


def test_partial_commit_stray_points_deleted_via_chunk_store(tmp_path):
    """A commit with points present but NO completion marker (a crash
    mid-flush) is reported missing, and its stray points are genuinely
    deleted from chunks.db -- proven by reading the store back afterward,
    not just trusting the return value."""
    vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
    shard_dir = _make_chunks_db_shard(vector_store, SHARD_2024Q1)

    store = ChunkStore(shard_dir / "chunks.db", expected_dim=VECTOR_DIM)
    try:
        store.write_batch(
            [
                {
                    "id": "proj:commit:partial1:0",
                    "vector": [0.1] * VECTOR_DIM,
                    "payload": {"commit_hash": "partial1"},
                }
            ]
        )
    finally:
        store.close()
    # NO mark_completed() call -- this commit is genuinely partial.

    missing = reconcile_shard(
        vector_store, SHARD_2024Q1, [_commit("partial1")], MODEL_NAME
    )

    assert [c.hash for c in missing] == ["partial1"]

    verify_store = ChunkStore(shard_dir / "chunks.db", immutable=True)
    try:
        assert "proj:commit:partial1:0" not in verify_store.all_point_ids()
    finally:
        verify_store.close()


def test_stray_delete_failure_raises_stray_delete_failed_error(tmp_path):
    """A genuine stray-deletion failure (real read-only chunks.db file --
    the same verified-real fault-injection technique used for ChunkStore's
    own transactional-delete tests) must be translated into
    StrayDeleteFailedError -- the SAME fail-closed contract the legacy
    SHARDED_JSON path already provides."""
    vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
    shard_dir = _make_chunks_db_shard(vector_store, SHARD_2024Q1)

    store = ChunkStore(shard_dir / "chunks.db", expected_dim=VECTOR_DIM)
    try:
        store.write_batch(
            [
                {
                    "id": "proj:commit:partial1:0",
                    "vector": [0.1] * VECTOR_DIM,
                    "payload": {"commit_hash": "partial1"},
                }
            ]
        )
    finally:
        store.close()

    db_path = shard_dir / "chunks.db"
    os.chmod(db_path, stat.S_IRUSR)
    try:
        with pytest.raises(StrayDeleteFailedError):
            reconcile_shard(
                vector_store, SHARD_2024Q1, [_commit("partial1")], MODEL_NAME
            )
    finally:
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
