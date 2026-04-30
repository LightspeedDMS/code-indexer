"""
Tests for Bug #946 atomic write fixes.

Verifies that each fixed write site leaves the original file intact
when the write raises midway (simulating a process kill via partial content
then exception). Also verifies no temp files are leaked on both failure
and success paths, and that the successful write path produces valid output.

Patching strategies by site:
  Site 1 (save_incremental_update HNSW): instance-level patch on the index object
  Sites 2-4 (JSON writes): module-level json.dump patching via string target
  Site 5 (build_index HNSW): class-level patch on hnswlib.Index.save_index since
    build_index creates its own index internally

Sites covered:
  1. hnsw_index_manager.save_incremental_update — HNSW binary (active production crash)
  2. hnsw_index_manager.mark_stale — metadata JSON
  3. hnsw_index_manager._update_metadata — metadata JSON
  4. progressive_metadata._save_metadata — watermark JSON
  5. hnsw_index_manager.build_index — full-rebuild HNSW binary
"""

import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import numpy as np
import pytest

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VECTOR_DIM = 64  # small for speed


# ---------------------------------------------------------------------------
# Shared failure simulators
# ---------------------------------------------------------------------------


def _partial_write_then_raise(path: str) -> None:
    """Write partial binary content to path then raise OSError.

    Simulates a process kill that occurs after the file is opened but before
    the write completes. Used to patch index.save_index.
    """
    with open(path, "wb") as f:
        f.write(b"\x00" * 8)
    raise OSError("Simulated kill during HNSW binary write")


def _partial_json_then_raise(obj: Any, f: Any, **kwargs: Any) -> None:
    """Write an incomplete JSON fragment then raise OSError.

    Simulates a process kill mid json.dump.
    """
    f.write('{"partial": true, "incomplete":')
    raise OSError("Simulated kill during json.dump")


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_no_tmp_files(directory: Path) -> None:
    """Assert that no .tmp files exist in the given directory."""
    tmp_files = list(directory.glob("*.tmp"))
    assert tmp_files == [], f"Leaked tmp files in {directory}: {tmp_files}"


def _assert_file_unchanged(path: Path, original_bytes: bytes) -> None:
    """Assert that the file content has not changed from original_bytes."""
    current = path.read_bytes()
    assert current == original_bytes, (
        f"File {path.name} was corrupted after write failure.\n"
        f"  original size: {len(original_bytes)}, current size: {len(current)}"
    )


# ---------------------------------------------------------------------------
# Shared test flow helpers
# ---------------------------------------------------------------------------


def _verify_json_write_atomic(
    target_file: Path,
    directory: Path,
    patch_target: str,
    invoke_write: Callable[[], None],
) -> None:
    """Verify a JSON write site is atomic via module-level json.dump patching.

    Patches json.dump at the module level (via string target) to simulate a
    partial write followed by a process kill. Asserts the original file is
    unchanged and no .tmp files are leaked after the failure.

    Args:
        target_file: The JSON file that must not be corrupted.
        directory: Directory to check for leaked .tmp files.
        patch_target: Dotted module path to json.dump (e.g.
            "code_indexer.storage.hnsw_index_manager.json.dump").
        invoke_write: Zero-argument callable that triggers the write under test.
    """
    original_bytes = target_file.read_bytes()

    with patch(patch_target, side_effect=_partial_json_then_raise):
        with pytest.raises(OSError, match="Simulated kill"):
            invoke_write()

    _assert_file_unchanged(target_file, original_bytes)
    _assert_no_tmp_files(directory)


def _verify_hnsw_write_atomic(
    index_file: Path,
    directory: Path,
    invoke_write: Callable[[], None],
) -> None:
    """Verify an HNSW binary write is atomic via class-level patching of hnswlib.

    Patches hnswlib.Index.save_index at the class level (an external dependency,
    not the SUT) so the simulated failure applies to any instance created or used
    inside the SUT. This avoids patching private SUT methods and works around the
    read-only C extension attribute limitation on individual hnswlib instances.

    Args:
        index_file: The hnsw_index.bin file that must not be corrupted.
        directory: Directory to check for leaked .tmp files.
        invoke_write: Zero-argument callable that triggers the write under test.
    """
    import hnswlib

    original_bytes = index_file.read_bytes()
    assert len(original_bytes) > 0, "Precondition: index file must exist with content"

    with patch.object(hnswlib.Index, "save_index", side_effect=_partial_write_then_raise):
        with pytest.raises(OSError, match="Simulated kill"):
            invoke_write()

    _assert_file_unchanged(index_file, original_bytes)
    _assert_no_tmp_files(directory)


# ---------------------------------------------------------------------------
# Fixtures / factory helpers
# ---------------------------------------------------------------------------


def _make_collection(tmp_path: Path) -> Path:
    """Create a minimal collection directory with collection_meta.json."""
    col = tmp_path / "test_collection"
    col.mkdir()
    meta = {
        "hnsw_index": {
            "version": 1,
            "index_rebuild_uuid": "abc-123",
            "vector_count": 0,
            "vector_dim": VECTOR_DIM,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2026-01-01T00:00:00+00:00",
            "file_size_bytes": 0,
            "id_mapping": {},
            "is_stale": False,
            "last_marked_stale": None,
        }
    }
    (col / "collection_meta.json").write_text(json.dumps(meta, indent=2))
    return col


def _make_hnsw_index(col: Path) -> Any:
    """Build a tiny real HNSW index and save it so the binary exists on disk."""
    import hnswlib

    index = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
    index.init_index(max_elements=10, ef_construction=50, M=8)
    rng = np.random.default_rng(0)
    vectors = rng.random((3, VECTOR_DIM)).astype(np.float32)
    index.add_items(vectors, [0, 1, 2])
    index_file = col / "hnsw_index.bin"
    index.save_index(str(index_file))
    return index


# ---------------------------------------------------------------------------
# Site 1: save_incremental_update — HNSW binary
# (instance-level patching: caller supplies the index object)
# ---------------------------------------------------------------------------


class TestSaveIncrementalUpdateHNSWBinaryAtomic:
    """HNSW binary in save_incremental_update must not corrupt existing file on failure."""

    def test_hnsw_binary_atomic_on_failure(self, tmp_path: Path):
        """If index.save_index raises after partial write, hnsw_index.bin is unchanged."""
        col = _make_collection(tmp_path)
        index = _make_hnsw_index(col)
        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)

        _verify_hnsw_write_atomic(
            index_file=col / "hnsw_index.bin",
            directory=col,
            invoke_write=lambda: manager.save_incremental_update(
                index=index,
                collection_path=col,
                id_to_label={"point-0": 0, "point-1": 1, "point-2": 2},
                label_to_id={0: "point-0", 1: "point-1", 2: "point-2"},
                vector_count=3,
            ),
        )

    def test_successful_incremental_update_writes_loadable_index(self, tmp_path: Path):
        """Successful save_incremental_update produces a valid, loadable HNSW binary."""
        import hnswlib

        col = _make_collection(tmp_path)
        index = _make_hnsw_index(col)

        rng = np.random.default_rng(99)
        new_vec = rng.random((1, VECTOR_DIM)).astype(np.float32)
        index.add_items(new_vec, [3])

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
        manager.save_incremental_update(
            index=index,
            collection_path=col,
            id_to_label={"p0": 0, "p1": 1, "p2": 2, "p3": 3},
            label_to_id={0: "p0", 1: "p1", 2: "p2", 3: "p3"},
            vector_count=4,
        )

        # Must be reloadable as a valid hnswlib index with correct element count
        loaded = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
        loaded.load_index(str(col / "hnsw_index.bin"), max_elements=10)
        assert loaded.get_current_count() == 4
        _assert_no_tmp_files(col)


# ---------------------------------------------------------------------------
# Site 2: mark_stale — metadata JSON
# (module-level json.dump patching)
# ---------------------------------------------------------------------------


class TestMarkStaleMetadataAtomic:
    """mark_stale collection_meta.json must not corrupt original file on failure."""

    def test_json_write_atomic_on_failure(self, tmp_path: Path):
        """If json.dump raises after partial write, collection_meta.json is unchanged."""
        col = _make_collection(tmp_path)
        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)

        _verify_json_write_atomic(
            target_file=col / "collection_meta.json",
            directory=col,
            patch_target="code_indexer.storage.hnsw_index_manager.json.dump",
            invoke_write=lambda: manager.mark_stale(col),
        )

    def test_mark_stale_success_sets_is_stale_true(self, tmp_path: Path):
        """Successful mark_stale sets is_stale=True in metadata and leaves no tmp files."""
        col = _make_collection(tmp_path)
        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
        manager.mark_stale(col)

        meta = json.loads((col / "collection_meta.json").read_text())
        assert meta["hnsw_index"]["is_stale"] is True
        _assert_no_tmp_files(col)


# ---------------------------------------------------------------------------
# Site 3: _update_metadata — metadata JSON
# (module-level json.dump patching)
# ---------------------------------------------------------------------------


class TestUpdateMetadataAtomic:
    """_update_metadata collection_meta.json must not corrupt original file on failure."""

    def test_json_write_atomic_on_failure(self, tmp_path: Path):
        """If json.dump raises after partial write during _update_metadata, file is unchanged."""
        col = _make_collection(tmp_path)
        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)

        _verify_json_write_atomic(
            target_file=col / "collection_meta.json",
            directory=col,
            patch_target="code_indexer.storage.hnsw_index_manager.json.dump",
            invoke_write=lambda: manager._update_metadata(
                collection_path=col,
                vector_count=3,
                M=16,
                ef_construction=200,
                ids=["id-0", "id-1", "id-2"],
                index_file_size=1024,
            ),
        )

    def test_update_metadata_success_writes_vector_count(self, tmp_path: Path):
        """Successful _update_metadata writes correct vector_count and leaves no tmp files."""
        col = _make_collection(tmp_path)
        (col / "hnsw_index.bin").write_bytes(b"\x00" * 256)  # fake binary for stat

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
        manager._update_metadata(
            collection_path=col,
            vector_count=7,
            M=16,
            ef_construction=200,
            ids=[f"id-{i}" for i in range(7)],
            index_file_size=256,
        )

        meta = json.loads((col / "collection_meta.json").read_text())
        assert meta["hnsw_index"]["vector_count"] == 7
        _assert_no_tmp_files(col)


# ---------------------------------------------------------------------------
# Site 4: progressive_metadata._save_metadata — watermark JSON
# (module-level json.dump patching)
# ---------------------------------------------------------------------------


class TestProgressiveMetadataSaveAtomic:
    """_save_metadata must leave original watermark file intact on write failure."""

    def test_json_write_atomic_on_failure(self, tmp_path: Path):
        """If json.dump raises after partial write in _save_metadata, original is unchanged."""
        meta_path = tmp_path / ".code-indexer" / "index_metadata.json"
        meta_path.parent.mkdir(parents=True)
        original_content = {
            "status": "completed",
            "files_processed": 100,
            "chunks_indexed": 500,
        }
        meta_path.write_text(json.dumps(original_content, indent=2))

        pm = ProgressiveMetadata(meta_path)

        _verify_json_write_atomic(
            target_file=meta_path,
            directory=tmp_path / ".code-indexer",
            patch_target="code_indexer.services.progressive_metadata.json.dump",
            invoke_write=pm._save_metadata,
        )

    def test_save_metadata_success_persists_data(self, tmp_path: Path):
        """Successful _save_metadata persists in-memory metadata and leaves no tmp files."""
        meta_path = tmp_path / "index_metadata.json"
        meta_path.write_text(json.dumps({"status": "completed", "files_processed": 0}))

        pm = ProgressiveMetadata(meta_path)
        pm.metadata["files_processed"] = 42
        pm._save_metadata()

        persisted = json.loads(meta_path.read_text())
        assert persisted["files_processed"] == 42
        _assert_no_tmp_files(tmp_path)


# ---------------------------------------------------------------------------
# Site 5: build_index — full-rebuild HNSW binary
# (class-level patching: build_index creates its own hnswlib.Index internally)
# ---------------------------------------------------------------------------


class TestBuildIndexHNSWBinaryAtomic:
    """build_index HNSW binary save must not corrupt existing file on failure."""

    def test_hnsw_binary_atomic_on_failure(self, tmp_path: Path):
        """If save_index raises after partial write during build_index, original is unchanged.

        build_index constructs its own hnswlib.Index internally, so we patch
        hnswlib.Index.save_index at the class level so the patch applies to any
        instance created inside build_index.
        """
        import hnswlib

        col = _make_collection(tmp_path)
        # Place an existing binary so we can assert it is not corrupted
        _make_hnsw_index(col)
        original_bytes = (col / "hnsw_index.bin").read_bytes()
        assert len(original_bytes) > 0

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
        rng = np.random.default_rng(1)
        new_vectors = rng.random((5, VECTOR_DIM)).astype(np.float32)
        new_ids = [f"new-{i}" for i in range(5)]

        with patch.object(hnswlib.Index, "save_index", side_effect=_partial_write_then_raise):
            with pytest.raises(OSError, match="Simulated kill"):
                manager.build_index(
                    collection_path=col,
                    vectors=new_vectors,
                    ids=new_ids,
                )

        _assert_file_unchanged(col / "hnsw_index.bin", original_bytes)
        _assert_no_tmp_files(col)

    def test_successful_build_index_creates_loadable_binary(self, tmp_path: Path):
        """Successful build_index writes a binary that is loadable with hnswlib."""
        import hnswlib

        col = _make_collection(tmp_path)
        manager = HNSWIndexManager(vector_dim=VECTOR_DIM)

        rng = np.random.default_rng(3)
        vectors = rng.random((4, VECTOR_DIM)).astype(np.float32)
        ids = ["v0", "v1", "v2", "v3"]

        manager.build_index(
            collection_path=col,
            vectors=vectors,
            ids=ids,
        )

        index_file = col / "hnsw_index.bin"
        loaded = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
        loaded.load_index(str(index_file), max_elements=10)
        assert loaded.get_current_count() == 4
        _assert_no_tmp_files(col)
