"""Unit tests for HNSW branch isolation features.

TDD red phase: Tests written BEFORE implementation.

Tests for:
- rebuild_from_vectors() with visible_files parameter (ghost vector elimination)
- is_stale() with filtered metadata (prevents false-positive staleness)
- _branch_isolation_did_filtered_rebuild flag (prevents end_indexing() overwrite)
- rebuild_hnsw_filtered() method on FilesystemVectorStore
"""

import json
from pathlib import Path
from typing import Set, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _make_collection_meta(collection_path: Path, vector_dim: int = 128) -> None:
    """Create a minimal collection_meta.json for tests."""
    meta = {
        "name": "test_collection",
        "vector_size": vector_dim,
        "vector_dim": vector_dim,
        "created_at": "2025-01-01T00:00:00Z",
        "quantization_range": {"min": -0.75, "max": 0.75},
        "index_version": 1,
    }
    meta_file = collection_path / "collection_meta.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_file, "w") as f:
        json.dump(meta, f)


def _write_vector_file(
    collection_path: Path,
    point_id: str,
    file_path: str,
    vector_dim: int = 128,
) -> Path:
    """Write a minimal vector JSON file to a collection."""
    # Use a sub-path to simulate the quantized storage layout
    vector_subdir = collection_path / "vectors"
    vector_subdir.mkdir(parents=True, exist_ok=True)
    vector_file = vector_subdir / f"vector_{point_id}.json"
    data = {
        "id": point_id,
        "vector": np.random.randn(vector_dim).tolist(),
        "payload": {
            "path": file_path,
            "type": "content",
        },
    }
    with open(vector_file, "w") as f:
        json.dump(data, f)
    return vector_file


class TestRebuildFromVectorsVisibleFiles:
    """Tests for rebuild_from_vectors() visible_files parameter."""

    def test_rebuild_without_visible_files_loads_all_vectors(self, tmp_path: Path):
        """Backward compatibility: visible_files=None loads all vectors."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 vector files
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path)

        assert count == 5, f"Expected 5 vectors, got {count}"

    def test_rebuild_with_visible_files_filters_out_hidden_files(self, tmp_path: Path):
        """visible_files parameter filters out vectors not in the visible set."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 vector files: 3 visible, 2 hidden
        visible_paths = {"file_0.py", "file_1.py", "file_2.py"}
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path, visible_files=visible_paths
        )

        assert count == 3, f"Expected 3 visible vectors, got {count}"

    def test_rebuild_with_empty_visible_files_returns_zero(self, tmp_path: Path):
        """Empty visible_files set results in 0-vector index."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 3 vector files
        for i in range(3):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=set())

        assert count == 0, f"Expected 0 vectors, got {count}"

    def test_rebuild_with_visible_files_writes_filtered_metadata(self, tmp_path: Path):
        """Filtered rebuild writes metadata with filtered=true and counts."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 vector files: 3 visible
        visible_paths = {"file_0.py", "file_1.py", "file_2.py"}
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        manager.rebuild_from_vectors(collection_path, visible_files=visible_paths)

        # Check metadata
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        hnsw_info = metadata.get("hnsw_index", {})
        assert hnsw_info.get("filtered") is True, "Expected filtered=True in metadata"
        assert hnsw_info.get("visible_count") == 3, (
            f"Expected visible_count=3, got {hnsw_info.get('visible_count')}"
        )
        assert hnsw_info.get("total_on_disk") == 5, (
            f"Expected total_on_disk=5, got {hnsw_info.get('total_on_disk')}"
        )

    def test_rebuild_without_visible_files_does_not_write_filtered_metadata(
        self, tmp_path: Path
    ):
        """Non-filtered rebuild does NOT write filtered=true metadata."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        for i in range(3):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        manager.rebuild_from_vectors(collection_path)  # No visible_files

        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        hnsw_info = metadata.get("hnsw_index", {})
        # filtered should be absent or False
        assert not hnsw_info.get("filtered", False), (
            "Expected filtered to be absent or False for non-filtered rebuild"
        )

    def test_rebuild_with_visible_files_only_queryable_vectors_are_in_index(
        self, tmp_path: Path
    ):
        """After filtered rebuild, only visible-file vectors appear in query results."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Create fixed vectors for reproducibility
        np.random.seed(42)
        vectors = {}
        for i in range(5):
            v = np.random.randn(128).astype(np.float32)
            v = v / np.linalg.norm(v)  # Normalize
            vectors[f"file_{i}.py"] = (f"vec_{i}", v)

        # Write vector files
        vector_subdir = collection_path / "vectors"
        vector_subdir.mkdir(parents=True, exist_ok=True)
        for file_path, (point_id, vec) in vectors.items():
            vector_file = vector_subdir / f"vector_{point_id}.json"
            data = {
                "id": point_id,
                "vector": vec.tolist(),
                "payload": {"path": file_path, "type": "content"},
            }
            with open(vector_file, "w") as f:
                json.dump(data, f)

        # Only show files 0, 1, 2
        visible_paths = {"file_0.py", "file_1.py", "file_2.py"}

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        count = manager.rebuild_from_vectors(collection_path, visible_files=visible_paths)
        assert count == 3

        # Query - result count should be at most 3
        index = manager.load_index(collection_path)
        assert index is not None

        query_vec = np.random.randn(128).astype(np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)

        result_ids, _ = manager.query(index, query_vec, collection_path, k=10)
        assert len(result_ids) <= 3, (
            f"Expected at most 3 results (only visible files), got {len(result_ids)}"
        )


class TestIsStaleFilteredMetadata:
    """Tests for is_stale() with filtered rebuild metadata."""

    def test_is_stale_returns_false_when_filtered_and_visible_count_matches(
        self, tmp_path: Path
    ):
        """is_stale() returns False when filtered=True and HNSW count equals visible_count."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 disk vectors
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        # Simulate a filtered rebuild: metadata says filtered=True, visible_count=3
        # HNSW vector_count (in index) = 3, but disk has 5
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        metadata["hnsw_index"] = {
            "version": 1,
            "vector_count": 3,  # HNSW index has 3 vectors
            "vector_dim": 128,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2025-01-01T00:00:00Z",
            "file_size_bytes": 1000,
            "id_mapping": {"0": "vec_0", "1": "vec_1", "2": "vec_2"},
            "is_stale": False,
            "last_marked_stale": None,
            "filtered": True,
            "visible_count": 3,
            "total_on_disk": 5,
        }
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        result = manager.is_stale(collection_path)

        assert result is False, (
            "is_stale() should return False when filtered=True and HNSW count matches visible_count"
        )

    def test_is_stale_returns_true_when_filtered_but_visible_count_mismatches(
        self, tmp_path: Path
    ):
        """is_stale() returns True when filtered=True but HNSW count != visible_count."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 disk vectors
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        # Simulate stale: HNSW has 2 but visible_count should be 3
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        metadata["hnsw_index"] = {
            "version": 1,
            "vector_count": 2,  # Wrong count - stale
            "vector_dim": 128,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2025-01-01T00:00:00Z",
            "file_size_bytes": 1000,
            "id_mapping": {"0": "vec_0", "1": "vec_1"},
            "is_stale": False,
            "last_marked_stale": None,
            "filtered": True,
            "visible_count": 3,  # Mismatch with vector_count=2
            "total_on_disk": 5,
        }
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        result = manager.is_stale(collection_path)

        assert result is True, (
            "is_stale() should return True when filtered=True but HNSW count != visible_count"
        )

    def test_is_stale_without_filtered_metadata_uses_disk_count(self, tmp_path: Path):
        """is_stale() without filtered metadata compares HNSW count against disk count."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 disk vectors
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        # Metadata says 5 vectors (matches disk), no filtered flag
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        metadata["hnsw_index"] = {
            "version": 1,
            "vector_count": 5,  # Matches disk count
            "vector_dim": 128,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2025-01-01T00:00:00Z",
            "file_size_bytes": 1000,
            "id_mapping": {str(i): f"vec_{i}" for i in range(5)},
            "is_stale": False,
            "last_marked_stale": None,
            # NO filtered, visible_count, or total_on_disk keys
        }
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        result = manager.is_stale(collection_path)

        assert result is False, (
            "is_stale() should return False when HNSW count matches disk count (no filtered)"
        )

    def test_is_stale_without_filtered_returns_true_on_disk_count_mismatch(
        self, tmp_path: Path
    ):
        """is_stale() without filtered returns True when disk count mismatches HNSW count."""
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path, vector_dim=128)

        # Write 5 disk vectors
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        # Metadata says 3 vectors (mismatch with disk=5), no filtered flag
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        metadata["hnsw_index"] = {
            "version": 1,
            "vector_count": 3,  # Mismatch with disk=5
            "vector_dim": 128,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2025-01-01T00:00:00Z",
            "file_size_bytes": 1000,
            "id_mapping": {str(i): f"vec_{i}" for i in range(3)},
            "is_stale": False,
            "last_marked_stale": None,
        }
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        manager = HNSWIndexManager(vector_dim=128, space="cosine")
        result = manager.is_stale(collection_path)

        assert result is True, (
            "is_stale() should return True when HNSW count mismatches disk count (no filtered)"
        )


class TestBranchIsolationDidFilteredRebuildFlag:
    """Tests for _branch_isolation_did_filtered_rebuild flag in FilesystemVectorStore."""

    def test_end_indexing_flag_defaults_to_false(self, tmp_path: Path):
        """_branch_isolation_did_filtered_rebuild defaults to False on initialization."""
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)

        assert hasattr(store, "_branch_isolation_did_filtered_rebuild"), (
            "FilesystemVectorStore should have _branch_isolation_did_filtered_rebuild attribute"
        )
        assert store._branch_isolation_did_filtered_rebuild is False, (
            "Flag should default to False"
        )

    def test_end_indexing_skips_rebuild_when_flag_is_true(self, tmp_path: Path):
        """end_indexing() skips HNSW rebuild when _branch_isolation_did_filtered_rebuild=True.

        We verify this by: doing a filtered rebuild (sets the flag),
        then calling end_indexing() without adding any vectors, and confirming
        the HNSW metadata's vector_count was NOT changed by a new full rebuild.
        """
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
        store.create_collection("test_collection", vector_size=128)

        # Write some vector files so a rebuild would produce a non-zero count
        collection_path = tmp_path / "test_collection"
        for i in range(3):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        # Do a "filtered" rebuild with 2 visible files by calling rebuild_hnsw_filtered
        # This sets _branch_isolation_did_filtered_rebuild = True
        # and the HNSW metadata will show 2 vectors
        visible_files = {"file_0.py", "file_1.py"}
        store.rebuild_hnsw_filtered("test_collection", visible_files=visible_files)

        assert store._branch_isolation_did_filtered_rebuild is True, (
            "Flag should be True after rebuild_hnsw_filtered()"
        )

        # Check HNSW metadata before calling end_indexing
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            before_meta = json.load(f)
        hnsw_before = before_meta.get("hnsw_index", {})
        rebuild_uuid_before = hnsw_before.get("index_rebuild_uuid")

        # Now call end_indexing - it should NOT overwrite the filtered rebuild
        store.end_indexing("test_collection")

        # After end_indexing, flag should be reset
        assert store._branch_isolation_did_filtered_rebuild is False, (
            "Flag should be reset to False after end_indexing()"
        )

        # The rebuild_uuid should NOT have changed (no new rebuild happened)
        with open(meta_file) as f:
            after_meta = json.load(f)
        hnsw_after = after_meta.get("hnsw_index", {})
        rebuild_uuid_after = hnsw_after.get("index_rebuild_uuid")

        assert rebuild_uuid_before == rebuild_uuid_after, (
            f"HNSW rebuild UUID changed unexpectedly: {rebuild_uuid_before} -> {rebuild_uuid_after}\n"
            f"end_indexing() must NOT overwrite a filtered rebuild"
        )

    def test_end_indexing_resets_flag_after_checking(self, tmp_path: Path):
        """end_indexing() resets _branch_isolation_did_filtered_rebuild to False after checking."""
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
        store.create_collection("test_collection", vector_size=128)

        # Directly set the flag
        store._branch_isolation_did_filtered_rebuild = True
        store.end_indexing("test_collection")

        # Flag should be reset to False
        assert store._branch_isolation_did_filtered_rebuild is False, (
            "Flag should be reset to False after end_indexing()"
        )


class TestRebuildHnswFiltered:
    """Tests for rebuild_hnsw_filtered() method on FilesystemVectorStore."""

    def test_rebuild_hnsw_filtered_exists_on_store(self, tmp_path: Path):
        """FilesystemVectorStore has rebuild_hnsw_filtered() method."""
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
        assert hasattr(store, "rebuild_hnsw_filtered"), (
            "FilesystemVectorStore should have rebuild_hnsw_filtered() method"
        )

    def test_rebuild_hnsw_filtered_produces_filtered_metadata(self, tmp_path: Path):
        """rebuild_hnsw_filtered() calls rebuild with visible_files and produces filtered metadata."""
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
        store.create_collection("test_collection", vector_size=128)

        # Write 5 vector files
        collection_path = tmp_path / "test_collection"
        for i in range(5):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        visible_files = {"file_0.py", "file_1.py"}
        store.rebuild_hnsw_filtered("test_collection", visible_files=visible_files)

        # Check that metadata has filtered=True and the correct counts
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file) as f:
            metadata = json.load(f)

        hnsw_info = metadata.get("hnsw_index", {})
        assert hnsw_info.get("filtered") is True, "Expected filtered=True in metadata"
        assert hnsw_info.get("visible_count") == 2, (
            f"Expected visible_count=2, got {hnsw_info.get('visible_count')}"
        )
        assert hnsw_info.get("total_on_disk") == 5, (
            f"Expected total_on_disk=5, got {hnsw_info.get('total_on_disk')}"
        )

    def test_rebuild_hnsw_filtered_sets_flag_on_store(self, tmp_path: Path):
        """rebuild_hnsw_filtered() sets _branch_isolation_did_filtered_rebuild=True."""
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
        store.create_collection("test_collection", vector_size=128)

        collection_path = tmp_path / "test_collection"
        for i in range(2):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        store.rebuild_hnsw_filtered("test_collection", visible_files={"file_0.py"})

        assert store._branch_isolation_did_filtered_rebuild is True, (
            "rebuild_hnsw_filtered() should set _branch_isolation_did_filtered_rebuild=True"
        )

    def test_rebuild_hnsw_filtered_invalidates_hnsw_cache_if_present(
        self, tmp_path: Path
    ):
        """rebuild_hnsw_filtered() calls hnsw_index_cache.invalidate() if cache is set."""
        mock_cache = MagicMock()
        store = FilesystemVectorStore(tmp_path, project_root=tmp_path, hnsw_index_cache=mock_cache)
        store.create_collection("test_collection", vector_size=128)

        collection_path = tmp_path / "test_collection"
        for i in range(2):
            _write_vector_file(collection_path, f"vec_{i}", f"file_{i}.py")

        store.rebuild_hnsw_filtered("test_collection", visible_files={"file_0.py"})

        # Cache invalidation should have been called
        mock_cache.invalidate.assert_called_once()
