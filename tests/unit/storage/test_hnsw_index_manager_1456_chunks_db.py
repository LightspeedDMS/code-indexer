"""HNSW rebuild + Bug #306 branch-visibility filter for CHUNKS_DB collections
(Story #1456 AC2, Epic #1454).

``HNSWIndexManager.rebuild_from_vectors`` is both the HNSW-rebuild source of
truth AND the Bug #306 branch-visibility filter reader. For a collection
whose ``collection_meta.json`` carries the ``chunks_db`` discriminator (see
``code_indexer.storage.shared.chunk_layout``), the rebuild must stream
vector+payload from ``chunks.db`` (via ``ChunkStore.stream_all()``) instead
of walking ``vector_*.json`` files -- and it must do so with ZERO
``vector_*.json`` files present on disk at all, and with the legacy
``Path.rglob("vector_*.json")`` scan never invoked (proving no rglob
fallback is silently used).
"""

import json
from pathlib import Path

import numpy as np

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
    """Build a CHUNKS_DB-layout collection: write chunks.db with `records`,
    THEN commit the discriminator LAST (mirrors AC1's mandatory ordering)."""
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
        "chunk_text": f"content for {file_path}",
    }


class TestRebuildFromChunksDbBasic:
    def test_rebuild_builds_index_with_zero_vector_json_files_present(
        self, tmp_path: Path
    ) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(5)]
        _make_chunks_db_collection(collection_path, records)

        # Prove no vector_*.json files exist anywhere under the collection --
        # any success here can ONLY come from streaming chunks.db.
        assert list(collection_path.rglob("vector_*.json")) == []

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 5
        index_file = collection_path / manager.INDEX_FILENAME
        assert index_file.exists()

    def test_rebuild_never_calls_rglob_vector_json_scan_for_chunks_db(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Direct proof (not just an empty-directory inference): the legacy
        Path.rglob("vector_*.json") scan is never invoked when the
        collection resolves to CHUNKS_DB."""
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(4)]
        _make_chunks_db_collection(collection_path, records)

        original_rglob = Path.rglob
        calls = []

        def spy_rglob(self, pattern, *args, **kwargs):
            calls.append(pattern)
            return original_rglob(self, pattern, *args, **kwargs)

        monkeypatch.setattr(Path, "rglob", spy_rglob)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 4
        assert "vector_*.json" not in calls


class TestRebuildFromChunksDbHiddenBranchesFilter:
    """Bug #306 branch-visibility filter equivalence for CHUNKS_DB."""

    def test_current_branch_excludes_hidden_vectors(self, tmp_path: Path) -> None:
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

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, current_branch="feature-x"
        )

        assert count == 3

    def test_current_branch_not_in_hidden_branches_includes_vector(
        self, tmp_path: Path
    ) -> None:
        collection_path = tmp_path / "test_coll"
        records = [
            _record("v0", "f0.py", 128, hidden_branches=["other-branch"]),
            _record("v1", "f1.py", 128),
        ]
        _make_chunks_db_collection(collection_path, records)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, current_branch="feature-x"
        )

        assert count == 2


class TestRebuildFromChunksDbVisibleFilesFilter:
    def test_visible_files_filters_out_hidden_paths(self, tmp_path: Path) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(5)]
        _make_chunks_db_collection(collection_path, records)

        visible = {"file_0.py", "file_1.py", "file_2.py"}
        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=visible)

        assert count == 3

    def test_empty_visible_files_returns_zero(self, tmp_path: Path) -> None:
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(3)]
        _make_chunks_db_collection(collection_path, records)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=set())

        assert count == 0


class TestRebuildFromChunksDbEmptyStore:
    def test_empty_chunks_db_returns_zero_no_crash(self, tmp_path: Path) -> None:
        collection_path = tmp_path / "test_coll"
        _make_chunks_db_collection(collection_path, records=[])

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 0


class TestRebuildFromVectorsLayoutOverride:
    """Story #1456 AC1: a FRESH consolidated-collection build writes chunks.db
    + builds its HNSW index BEFORE the discriminator is committed (the
    discriminator commit must be the mandatory FINAL step). Since
    resolve_chunk_layout() would still say SHARDED_JSON at that point (no
    discriminator on disk yet), the fresh-build orchestrator needs an
    explicit override to force CHUNKS_DB streaming without waiting for the
    resolver to see a file that doesn't exist yet."""

    def test_layout_override_forces_chunks_db_streaming_without_discriminator(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.chunk_layout import ChunkLayout

        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(4)]
        # Build chunks.db WITHOUT committing the discriminator -- mirrors the
        # in-progress fresh-build window.
        _make_collection_meta(collection_path, vector_dim=128)
        store = ChunkStore(collection_path / "chunks.db")
        try:
            store.write_batch(records)
        finally:
            store.close()

        assert list(collection_path.rglob("vector_*.json")) == []

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, layout_override=ChunkLayout.CHUNKS_DB
        )

        assert count == 4

    def test_no_override_without_discriminator_finds_zero_legacy_files(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: WITHOUT the override, the same not-yet-flagged
        collection resolves SHARDED_JSON (fail-closed) and finds nothing,
        proving the override is what makes the fresh-build ordering work."""
        collection_path = tmp_path / "test_coll"
        records = [_record(f"vec_{i}", f"file_{i}.py", 128) for i in range(4)]
        _make_collection_meta(collection_path, vector_dim=128)
        store = ChunkStore(collection_path / "chunks.db")
        try:
            store.write_batch(records)
        finally:
            store.close()

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 0
