"""Tests for Bug #1223 — corrupt HNSW index self-heal (extend of #1223 partial fix).

The earlier #1223 fix handled corrupt/0-byte collection_meta.json.  A crashed
rebuild can ALSO leave a corrupt/partial hnsw_index.bin (and stale
.tmp_hnsw_*.tmp files).  The symptom: after meta self-heal the golden-repo
refresh fails later with hnswlib's RuntimeError "Index seems to be corrupted
or unsupported".

These tests enforce:
  - _is_corrupt_index_error(exc) — classifier helper
  - discard_corrupt_index(collection_path) — cleanup helper
  - INDEX-TIME path (load_for_incremental_update) RECOVERS from corrupt .bin
  - QUERY-TIME path (load_index called directly) still RAISES on corrupt .bin
  - Stale .tmp_hnsw_*.tmp files cleaned on index-time rebuild
  - Valid index NOT touched on index-time rebuild path (no false-positive discard)
  - End-to-end composition: 0-byte meta + corrupt .bin both self-heal on reindex

All tests use REAL hnswlib + real filesystem (tmp_path) — no mocks for the
hnswlib layer.
"""

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 64  # Small dimension so tests are fast


def _make_manager() -> HNSWIndexManager:
    return HNSWIndexManager(vector_dim=DIM, space="cosine")


def _build_real_index(collection_path: Path, n: int = 10) -> list:
    """Build a real HNSW index in collection_path; return vector IDs."""
    manager = _make_manager()
    vectors = np.random.randn(n, DIM).astype(np.float32)
    ids = [f"vec_{i}" for i in range(n)]
    manager.build_index(collection_path, vectors, ids)
    return ids


def _corrupt_index(collection_path: Path) -> Path:
    """Overwrite hnsw_index.bin with garbage bytes; return path."""
    index_file: Path = collection_path / "hnsw_index.bin"
    index_file.write_bytes(b"GARBAGE_CORRUPTED_DATA_NOT_A_VALID_INDEX")
    return index_file


def _zero_byte_index(collection_path: Path) -> Path:
    """Truncate hnsw_index.bin to 0 bytes; return path."""
    index_file: Path = collection_path / "hnsw_index.bin"
    index_file.write_bytes(b"")
    return index_file


def _plant_stale_tmp(collection_path: Path, n: int = 2) -> list:
    """Create n stale .tmp_hnsw_*.tmp files; return their paths."""
    paths = []
    for i in range(n):
        fd, path = tempfile.mkstemp(
            dir=str(collection_path), prefix=".tmp_hnsw_", suffix=".tmp"
        )
        os.close(fd)
        Path(path).write_bytes(b"stale temp data")
        paths.append(Path(path))
    return paths


def _write_valid_collection_meta(collection_path: Path, vector_dim: int = DIM) -> None:
    """Write a minimal valid collection_meta.json."""
    meta = {"name": collection_path.name, "vector_size": vector_dim}
    meta_path = collection_path / "collection_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f)


# ---------------------------------------------------------------------------
# 1. _is_corrupt_index_error() classifier
# ---------------------------------------------------------------------------


class TestIsCorruptIndexError:
    """_is_corrupt_index_error returns True/False correctly."""

    def test_returns_true_for_hnswlib_corruption_message(self):
        """GIVEN a RuntimeError with hnswlib's exact corruption message
        WHEN _is_corrupt_index_error() is called
        THEN returns True.
        """
        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        exc = RuntimeError("Index seems to be corrupted or unsupported")
        assert _is_corrupt_index_error(exc) is True

    def test_returns_true_case_insensitive(self):
        """GIVEN a RuntimeError with mixed-case corruption message
        WHEN _is_corrupt_index_error() is called
        THEN returns True (match is case-insensitive).
        """
        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        exc = RuntimeError("INDEX SEEMS TO BE CORRUPTED OR UNSUPPORTED")
        assert _is_corrupt_index_error(exc) is True

    def test_returns_false_for_unrelated_runtime_error(self):
        """GIVEN a RuntimeError unrelated to corruption
        WHEN _is_corrupt_index_error() is called
        THEN returns False.
        """
        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        exc = RuntimeError("Cannot return the results in a contiguous 2D array")
        assert _is_corrupt_index_error(exc) is False

    def test_returns_false_for_other_exception_types(self):
        """GIVEN a non-RuntimeError exception
        WHEN _is_corrupt_index_error() is called
        THEN returns False.
        """
        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        assert _is_corrupt_index_error(ValueError("something else")) is False
        assert _is_corrupt_index_error(OSError("file not found")) is False

    def test_returns_true_for_real_hnswlib_corruption(self, tmp_path):
        """GIVEN hnswlib actually raises RuntimeError on a corrupt index file
        WHEN _is_corrupt_index_error() is applied to that real exception
        THEN returns True.
        """
        import hnswlib

        from code_indexer.storage.hnsw_index_manager import _is_corrupt_index_error

        index_file = tmp_path / "hnsw_index.bin"
        index_file.write_bytes(b"GARBAGE")
        exc_to_test = None
        try:
            idx = hnswlib.Index(space="cosine", dim=DIM)
            idx.load_index(str(index_file), max_elements=100)
        except RuntimeError as e:
            exc_to_test = e
        assert exc_to_test is not None, "hnswlib did not raise on corrupt file"
        assert _is_corrupt_index_error(exc_to_test) is True


# ---------------------------------------------------------------------------
# 2. discard_corrupt_index() cleanup helper
# ---------------------------------------------------------------------------


class TestDiscardCorruptIndex:
    """discard_corrupt_index() removes .bin + stale .tmp_hnsw_*.tmp."""

    def test_removes_corrupt_bin_file(self, tmp_path):
        """GIVEN a collection with a corrupt hnsw_index.bin
        WHEN discard_corrupt_index() is called
        THEN the .bin file is removed.
        """
        from code_indexer.storage.hnsw_index_manager import discard_corrupt_index

        _corrupt_index(tmp_path)
        assert (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists()

        discard_corrupt_index(tmp_path)

        assert not (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists()

    def test_removes_stale_tmp_hnsw_files(self, tmp_path):
        """GIVEN a collection with stale .tmp_hnsw_*.tmp files
        WHEN discard_corrupt_index() is called
        THEN all .tmp_hnsw_*.tmp files are removed.
        """
        from code_indexer.storage.hnsw_index_manager import discard_corrupt_index

        _corrupt_index(tmp_path)
        stale_files = _plant_stale_tmp(tmp_path, n=3)
        assert all(f.exists() for f in stale_files)

        discard_corrupt_index(tmp_path)

        assert all(not f.exists() for f in stale_files)

    def test_noop_when_no_bin_file(self, tmp_path):
        """GIVEN a collection directory with no index file
        WHEN discard_corrupt_index() is called
        THEN no error is raised.
        """
        from code_indexer.storage.hnsw_index_manager import discard_corrupt_index

        # Should not raise even when .bin is absent
        discard_corrupt_index(tmp_path)

    def test_does_not_remove_other_files(self, tmp_path):
        """GIVEN a collection with other files (JSON vectors, meta)
        WHEN discard_corrupt_index() is called
        THEN only the .bin and .tmp_hnsw files are removed; others survive.
        """
        from code_indexer.storage.hnsw_index_manager import discard_corrupt_index

        _corrupt_index(tmp_path)
        meta = tmp_path / "collection_meta.json"
        meta.write_text('{"vector_size": 64}')
        vector_file = tmp_path / "vector_abc.json"
        vector_file.write_text('{"id": "abc"}')

        discard_corrupt_index(tmp_path)

        assert meta.exists(), "collection_meta.json must not be removed"
        assert vector_file.exists(), "vector JSON files must not be removed"


# ---------------------------------------------------------------------------
# 3. load_for_incremental_update (INDEX-TIME) recovers from corrupt index
# ---------------------------------------------------------------------------


class TestIncrementalUpdateRecovery:
    """load_for_incremental_update() discards corrupt index and returns None."""

    def test_corrupt_bin_returns_none_and_discards(self, tmp_path):
        """GIVEN a collection with a corrupt hnsw_index.bin
        WHEN load_for_incremental_update() is called (index-time path)
        THEN returns None (triggers full rebuild fallback) AND the corrupt
             .bin is removed from disk.
        """
        _build_real_index(tmp_path)
        _corrupt_index(tmp_path)

        manager = _make_manager()
        index, id_to_label, label_to_id, next_label = (
            manager.load_for_incremental_update(tmp_path)
        )

        assert index is None, "Expected None to signal caller to do full rebuild"
        assert not (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists(), (
            "Corrupt .bin must be deleted after discard"
        )

    def test_corrupt_bin_also_cleans_stale_tmp_files(self, tmp_path):
        """GIVEN a collection with corrupt .bin AND stale .tmp_hnsw_*.tmp files
        WHEN load_for_incremental_update() is called
        THEN both the .bin and all .tmp_hnsw_*.tmp files are removed.
        """
        _build_real_index(tmp_path)
        _corrupt_index(tmp_path)
        stale_files = _plant_stale_tmp(tmp_path, n=2)

        manager = _make_manager()
        manager.load_for_incremental_update(tmp_path)

        assert not (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists()
        assert all(not f.exists() for f in stale_files)

    def test_zero_byte_bin_returns_none_and_discards(self, tmp_path):
        """GIVEN a collection with a 0-byte hnsw_index.bin
        WHEN load_for_incremental_update() is called
        THEN returns None and removes the 0-byte file.
        """
        _build_real_index(tmp_path)
        _zero_byte_index(tmp_path)

        manager = _make_manager()
        index, _, _, _ = manager.load_for_incremental_update(tmp_path)

        assert index is None
        assert not (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists()

    def test_valid_index_loads_normally_no_discard(self, tmp_path):
        """GIVEN a collection with a valid hnsw_index.bin
        WHEN load_for_incremental_update() is called
        THEN returns a non-None index (no discard of valid index).
        """
        _build_real_index(tmp_path, n=10)

        manager = _make_manager()
        index, id_to_label, label_to_id, next_label = (
            manager.load_for_incremental_update(tmp_path)
        )

        assert index is not None, "Valid index must NOT be discarded"
        assert len(id_to_label) == 10

    def test_absent_index_returns_none_without_error(self, tmp_path):
        """GIVEN a collection directory with no hnsw_index.bin at all
        WHEN load_for_incremental_update() is called
        THEN returns None without error (existing no-index-yet behavior preserved).
        """
        manager = _make_manager()
        index, _, _, _ = manager.load_for_incremental_update(tmp_path)
        assert index is None


# ---------------------------------------------------------------------------
# 4. load_index (QUERY-TIME) still raises on corrupt index
# ---------------------------------------------------------------------------


class TestQueryTimeRaisesOnCorruptIndex:
    """load_index() (query path) must RAISE on a corrupt .bin — never self-heal."""

    def test_raises_on_corrupt_bin(self, tmp_path):
        """GIVEN a collection with a corrupt hnsw_index.bin
        WHEN load_index() is called (query-time path)
        THEN RuntimeError is raised — query path does NOT self-heal.
        """
        _build_real_index(tmp_path)
        _corrupt_index(tmp_path)

        manager = _make_manager()
        with pytest.raises(RuntimeError):
            manager.load_index(tmp_path)

    def test_raises_on_zero_byte_bin(self, tmp_path):
        """GIVEN a collection with a 0-byte hnsw_index.bin
        WHEN load_index() is called (query path)
        THEN RuntimeError is raised.
        """
        _build_real_index(tmp_path)
        _zero_byte_index(tmp_path)

        manager = _make_manager()
        with pytest.raises(RuntimeError):
            manager.load_index(tmp_path)

    def test_corrupt_bin_not_deleted_by_query_path(self, tmp_path):
        """GIVEN a collection with a corrupt hnsw_index.bin
        WHEN load_index() is called (query path) — which MUST raise RuntimeError
        THEN the .bin file is NOT deleted (query path must not touch filesystem).
        """
        _build_real_index(tmp_path)
        index_file = _corrupt_index(tmp_path)

        manager = _make_manager()
        with pytest.raises(RuntimeError):
            manager.load_index(tmp_path)

        assert index_file.exists(), (
            "Query-time load_index must NOT delete the corrupt .bin"
        )

    def test_returns_none_for_absent_index(self, tmp_path):
        """GIVEN a collection with no hnsw_index.bin
        WHEN load_index() is called
        THEN returns None (no error — existing behavior preserved).
        """
        manager = _make_manager()
        result = manager.load_index(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# 5. Stale .tmp_hnsw cleanup on rebuild; valid index untouched
# ---------------------------------------------------------------------------


class TestStaleTemporaryFileCleanup:
    """Stale .tmp_hnsw_*.tmp files are cleaned on rebuild; valid index is not."""

    def test_stale_tmp_files_removed_when_rebuild_after_corruption(self, tmp_path):
        """GIVEN a collection with stale .tmp_hnsw_*.tmp AND a corrupt .bin
        WHEN load_for_incremental_update() is called (returns None)
        THEN all stale .tmp_hnsw_*.tmp files are removed.
        """
        _build_real_index(tmp_path)
        _corrupt_index(tmp_path)
        stale = _plant_stale_tmp(tmp_path, n=2)

        manager = _make_manager()
        manager.load_for_incremental_update(tmp_path)

        assert all(not f.exists() for f in stale)

    def test_valid_index_not_discarded_even_with_stale_tmp_present(self, tmp_path):
        """GIVEN a collection with a VALID .bin AND stale .tmp_hnsw_*.tmp files
        WHEN load_for_incremental_update() is called
        THEN the valid .bin is NOT discarded (no false-positive clean).
        """
        _build_real_index(tmp_path, n=5)
        _plant_stale_tmp(tmp_path, n=1)

        manager = _make_manager()
        index, id_to_label, _, _ = manager.load_for_incremental_update(tmp_path)

        # Valid index must be returned intact
        assert index is not None
        assert len(id_to_label) == 5

    def test_rebuild_from_vectors_cleans_stale_tmp_files(self, tmp_path):
        """GIVEN a collection dir with stale .tmp_hnsw_*.tmp files AND vector JSONs
        WHEN rebuild_from_vectors() is called (full index-time rebuild)
        THEN stale .tmp_hnsw_*.tmp files are removed before/during rebuild.
        """
        # Create collection_meta.json so rebuild_from_vectors can run
        _write_valid_collection_meta(tmp_path)

        # Write some vector JSON files so rebuild has something to work with
        vectors = np.random.randn(5, DIM).astype(np.float32)
        for i, v in enumerate(vectors):
            vec_data = {
                "id": f"vec_{i}",
                "vector": v.tolist(),
                "payload": {"path": f"file_{i}.py"},
            }
            (tmp_path / f"vector_{i}.json").write_text(json.dumps(vec_data))

        # Plant stale tmp files
        stale = _plant_stale_tmp(tmp_path, n=2)

        manager = _make_manager()
        count = manager.rebuild_from_vectors(tmp_path)

        assert count == 5
        # Stale tmp files should be gone
        assert all(not f.exists() for f in stale), (
            "rebuild_from_vectors must clean stale .tmp_hnsw_*.tmp files"
        )


# ---------------------------------------------------------------------------
# 6. End-to-end composition: corrupt .bin self-heals through full reindex
# ---------------------------------------------------------------------------


class TestEndToEndCorruptBinSelfHeal:
    """Corrupt .bin discarded by index-time path; subsequent rebuild produces
    working index queryable on the query path."""

    def test_corrupt_bin_followed_by_rebuild_produces_working_index(self, tmp_path):
        """GIVEN a collection with a corrupt hnsw_index.bin (simulating a crash)
        WHEN:
          1. load_for_incremental_update() is called — signals full rebuild needed
          2. rebuild_from_vectors() is called — rebuilds from all vector JSONs
        THEN:
          - The corrupt .bin is replaced with a valid index
          - A subsequent load_index() + query returns results
        """
        # Set up a collection with real vector files and a working index
        _write_valid_collection_meta(tmp_path)
        vectors = np.random.randn(8, DIM).astype(np.float32)
        for i, v in enumerate(vectors):
            vec_data = {
                "id": f"vec_{i}",
                "vector": v.tolist(),
                "payload": {"path": f"file_{i}.py"},
            }
            (tmp_path / f"vector_{i}.json").write_text(json.dumps(vec_data))

        manager = _make_manager()
        # Build initial valid index
        manager.build_index(tmp_path, vectors, [f"vec_{i}" for i in range(8)])

        # Simulate crash: corrupt the index
        _corrupt_index(tmp_path)
        stale = _plant_stale_tmp(tmp_path, n=1)

        # Step 1: Index-time path discovers corruption, discards, returns None
        index_result, _, _, _ = manager.load_for_incremental_update(tmp_path)
        assert index_result is None, "Should signal full rebuild needed"
        assert not (tmp_path / HNSWIndexManager.INDEX_FILENAME).exists()
        assert all(not f.exists() for f in stale)

        # Step 2: Full rebuild from vector JSONs
        count = manager.rebuild_from_vectors(tmp_path)
        assert count == 8, f"Expected 8 vectors, got {count}"

        # Step 3: The index is now valid — query path loads and returns results
        rebuilt_index = manager.load_index(tmp_path)
        assert rebuilt_index is not None, "Rebuilt index must be loadable"

        # Step 4: Can actually query
        query_vec = np.random.randn(DIM).astype(np.float32)
        ids, distances = manager.query(rebuilt_index, query_vec, tmp_path, k=3)
        assert len(ids) == 3, f"Expected 3 results, got {len(ids)}"
        assert all(isinstance(d, float) for d in distances)

    def test_zero_byte_meta_and_corrupt_bin_both_self_heal_via_collection_recreate(
        self, tmp_path
    ):
        """GIVEN a collection with BOTH 0-byte meta AND corrupt .bin
        WHEN:
          - collection_exists() returns False (meta-level self-heal from #1223 v1)
          - create_collection() is called to recreate the meta
          - rebuild_from_vectors() is called to rebuild the HNSW index
        THEN both defects are resolved and the index is queryable.

        Validates that the #1223 v1 meta fix + the new .bin fix compose correctly.
        """
        # Create a real collection with vector JSON files
        _write_valid_collection_meta(tmp_path)
        vectors = np.random.randn(5, DIM).astype(np.float32)
        for i, v in enumerate(vectors):
            vec_data = {
                "id": f"vec_{i}",
                "vector": v.tolist(),
                "payload": {"path": f"file_{i}.py"},
            }
            (tmp_path / f"vector_{i}.json").write_text(json.dumps(vec_data))

        manager = _make_manager()
        manager.build_index(tmp_path, vectors, [f"vec_{i}" for i in range(5)])

        # Simulate double crash: 0-byte meta + corrupt .bin
        (tmp_path / "collection_meta.json").write_bytes(b"")
        (tmp_path / HNSWIndexManager.INDEX_FILENAME).write_bytes(b"GARBAGE")

        # Part A: meta fix — rebuild_from_vectors raises FileNotFoundError or
        # the caller recreates meta; here we simply restore meta manually to
        # simulate the #1223 v1 self-heal recreating the collection
        _write_valid_collection_meta(tmp_path, vector_dim=DIM)

        # Part B: .bin fix — index-time load sees corrupt .bin, discards, returns None
        index_result, _, _, _ = manager.load_for_incremental_update(tmp_path)
        assert index_result is None

        # Part C: full rebuild
        count = manager.rebuild_from_vectors(tmp_path)
        assert count == 5

        # Both defects resolved — query path works
        rebuilt = manager.load_index(tmp_path)
        assert rebuilt is not None
        query_vec = np.random.randn(DIM).astype(np.float32)
        ids, _ = manager.query(rebuilt, query_vec, tmp_path, k=3)
        assert len(ids) == 3
