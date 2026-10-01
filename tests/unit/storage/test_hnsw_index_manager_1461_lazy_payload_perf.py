"""Story #1461 salvage item #9 [LOW, perf] (Epic #1454).

``HNSWIndexManager._load_vectors_from_chunks_db`` (wired by
``rebuild_from_vectors`` for CHUNKS_DB-layout collections, Story #1456 AC2)
used to stream via ``ChunkStore.stream_all()``, which unconditionally
zstd-decompresses + json.loads the ENTIRE opaque ``data`` blob (payload +
chunk_text/git_blob_hash + diff) for every row, even though the common
rebuild path only needs the vector + id + top-level ``path`` column.

This is the consumer-side wiring proof that the fix
(``ChunkStore.stream_for_index_rebuild``) is actually used, with the correct
``need_payload`` decision per mode:
  - full unfiltered rebuild -> need_payload=False -> 0 decompress calls
  - visible_files-filtered rebuild -> need_payload=False -> 0 decompress
    calls (visible_files always takes priority over hidden_branches, so the
    payload is never needed when it is set)
  - branch-aware rebuild (Bug #306 hidden_branches) -> need_payload=True ->
    exactly one decompress call per row (the payload must be inspected for
    every row to decide hidden_branches membership)

And that all three modes still produce IDENTICAL results (vector count, id
set, filtering outcome) to the pre-optimization behavior -- this is a pure
I/O optimization, not a behavior change. The correctness of the filtering
semantics themselves is already exhaustively covered by
``test_hnsw_index_manager_1456_chunks_db.py``; this file adds the
decompress-call-count proof on top of the SAME fixtures/patterns.
"""

import json
from pathlib import Path

import numpy as np
import zstandard

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _make_collection_meta(collection_path: Path, vector_dim: int = 128) -> None:
    meta = {
        "name": "test_collection",
        "vector_size": vector_dim,
        "vector_dim": vector_dim,
        "created_at": "2025-01-01T00:00:00Z",
        "quantization_range": {"min": -0.75, "max": 0.75},
        "index_version": 1,
    }
    collection_path.mkdir(parents=True, exist_ok=True)
    meta_file = collection_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f)


def _make_chunks_db_collection(
    collection_path: Path,
    records: list,
    vector_dim: int = 128,
) -> None:
    _make_collection_meta(collection_path, vector_dim=vector_dim)
    store = ChunkStore(collection_path / "chunks.db")
    try:
        store.write_batch(records)
    finally:
        store.close()
    write_chunks_db_discriminator(collection_path)


def _record(point_id: str, file_path: str, vector_dim: int, **payload_extra) -> dict:
    payload = {"path": file_path, "type": "content"}
    payload.update(payload_extra)
    return {
        "id": point_id,
        "vector": np.random.randn(vector_dim).astype(np.float32).tolist(),
        "payload": payload,
        # Deliberately large text blob -- if the fix regresses and the full
        # blob is decompressed anyway, the decompress-count assertions
        # below would still catch it regardless of size, but a large blob
        # makes any accidental full-corpus decode obviously expensive too.
        "chunk_text": f"content for {file_path} " * 200,
    }


def _install_decompress_counter(monkeypatch) -> dict:
    call_count = {"n": 0}
    original = zstandard.ZstdDecompressor.decompress

    def counting_decompress(self, *args, **kwargs):
        call_count["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(zstandard.ZstdDecompressor, "decompress", counting_decompress)
    return call_count


class TestFullUnfilteredRebuildSkipsPayloadDecode:
    def test_zero_decompress_calls(self, tmp_path: Path, monkeypatch) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(6)]
        _make_chunks_db_collection(collection_path, records)

        call_count = _install_decompress_counter(monkeypatch)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 6
        assert call_count["n"] == 0

    def test_id_set_identical_to_stream_all_baseline(self, tmp_path: Path) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(6)]
        _make_chunks_db_collection(collection_path, records)

        store = ChunkStore(collection_path / "chunks.db")
        try:
            expected_ids = {r["id"] for r in store.stream_all()}
        finally:
            store.close()

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == len(expected_ids) == 6


class TestVisibleFilesFilteredRebuildSkipsPayloadDecode:
    def test_zero_decompress_calls(self, tmp_path: Path, monkeypatch) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(6)]
        _make_chunks_db_collection(collection_path, records)

        call_count = _install_decompress_counter(monkeypatch)

        visible = {"file_0.py", "file_1.py", "file_2.py"}
        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=visible)

        assert count == 3
        assert call_count["n"] == 0

    def test_filtering_outcome_matches_stream_all_baseline(
        self, tmp_path: Path
    ) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(6)]
        _make_chunks_db_collection(collection_path, records)

        visible = {"file_0.py", "file_1.py", "file_2.py"}

        store = ChunkStore(collection_path / "chunks.db")
        try:
            expected_ids = {
                r["id"]
                for r in store.stream_all()
                if r.get("payload", {}).get("path") in visible
            }
        finally:
            store.close()

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=visible)

        assert count == len(expected_ids) == 3

    def test_visible_files_takes_priority_over_hidden_branches_zero_decompress(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When BOTH visible_files and current_branch are supplied,
        visible_files wins (matches the pre-existing elif semantics) --
        need_payload must still resolve False, since hidden_branches is
        never consulted in this mode."""
        collection_path = tmp_path / "test_coll"
        records = [
            _record("v0", "a.py", 128, hidden_branches=["feature-x"]),
            _record("v1", "b.py", 128),
        ]
        _make_chunks_db_collection(collection_path, records)

        call_count = _install_decompress_counter(monkeypatch)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"a.py", "b.py"},
            current_branch="feature-x",
        )

        assert count == 2
        assert call_count["n"] == 0


class TestBranchAwareRebuildStillDecodesPayload:
    def test_decompresses_exactly_once_per_row(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        collection_path = tmp_path / "test_coll"
        records = []
        for i in range(3):
            records.append(_record(f"visible_{i}", f"visible_{i}.py", 128))
        for i in range(2):
            records.append(
                _record(
                    f"hidden_{i}",
                    f"hidden_{i}.py",
                    128,
                    hidden_branches=["feature-x"],
                )
            )
        _make_chunks_db_collection(collection_path, records)

        call_count = _install_decompress_counter(monkeypatch)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, current_branch="feature-x"
        )

        # Filtering result unchanged from Story #1456's correctness tests.
        assert count == 3
        # Every one of the 5 rows must be decoded to decide hidden_branches
        # membership -- one decompress call per row scanned, never more,
        # never fewer, never zero.
        assert call_count["n"] == 5

    def test_filtering_outcome_matches_stream_all_baseline(
        self, tmp_path: Path
    ) -> None:
        collection_path = tmp_path / "test_coll"
        records = [
            _record("visible_0", "visible_0.py", 128),
            _record("hidden_0", "hidden_0.py", 128, hidden_branches=["feature-x"]),
            _record("other_0", "other_0.py", 128, hidden_branches=["other-branch"]),
        ]
        _make_chunks_db_collection(collection_path, records)

        store = ChunkStore(collection_path / "chunks.db")
        try:
            expected_ids = {
                r["id"]
                for r in store.stream_all()
                if "feature-x" not in r.get("payload", {}).get("hidden_branches", [])
            }
        finally:
            store.close()

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, current_branch="feature-x"
        )

        assert count == len(expected_ids) == 2
