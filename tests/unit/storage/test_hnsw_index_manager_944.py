"""Tests for bug #944: HNSW double-delete crash and non-atomic save.

Covers:
1. remove_vector() survives real hnswlib RuntimeError "already deleted"
2. remove_vector() propagates other RuntimeErrors
3. add_or_update_vector() survives real hnswlib RuntimeError "already deleted"
4. save_incremental_update() leaves no .tmp files after successful write
5. save_incremental_update() does not corrupt metadata file on crash between writes

The real hnswlib error message (from hnswalg.h:884) is:
    "The requested to delete element is already deleted"
Tests must use this exact message so the "already deleted" substring guard is
tested against the real signal, not a fabricated variant that happens to match
a different (wrong) substring.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


def _make_manager(vector_dim: int = 64) -> HNSWIndexManager:
    return HNSWIndexManager(vector_dim=vector_dim, space="cosine")


def _make_persisting_mock_index(current_count: int = 2) -> MagicMock:
    """Return a mock hnswlib.Index whose save_index writes a real binary file."""
    mock_index = MagicMock()
    mock_index.get_current_count.return_value = current_count

    def fake_save_index(path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(b"fake_hnsw_binary_data")

    mock_index.save_index.side_effect = fake_save_index
    return mock_index


class TestRemoveVectorAlreadyDeleted:
    """remove_vector() must survive "already been marked as deleted" RuntimeError."""

    def test_remove_vector_already_deleted_survives(self):
        """remove_vector() does not raise when mark_deleted raises the real hnswlib error."""
        manager = _make_manager()

        mock_index = MagicMock()
        mock_index.mark_deleted.side_effect = RuntimeError(
            "The requested to delete element is already deleted"
        )

        id_to_label = {"point_abc": 5}
        label_to_id = {5: "point_abc"}

        manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)

        assert "point_abc" not in id_to_label, (
            "point_abc must be removed from id_to_label even after swallowed exception"
        )
        assert 5 not in label_to_id, (
            "label 5 must be removed from label_to_id even after swallowed exception"
        )

    def test_remove_vector_other_runtime_error_propagates(self):
        """remove_vector() re-raises RuntimeErrors unrelated to double-delete."""
        manager = _make_manager()

        mock_index = MagicMock()
        mock_index.mark_deleted.side_effect = RuntimeError("some unrelated hnswlib internal error")

        id_to_label = {"point_abc": 5}
        label_to_id = {5: "point_abc"}

        with pytest.raises(RuntimeError, match="some unrelated hnswlib internal error"):
            manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)


class TestAddOrUpdateVectorAlreadyDeleted:
    """add_or_update_vector() must survive "already been marked as deleted" RuntimeError."""

    def test_add_or_update_vector_already_deleted_survives(self):
        """add_or_update_vector() does not raise when mark_deleted raises already-deleted error.

        The re-add (add_items) must still proceed after the swallowed exception.
        """
        manager = _make_manager()

        mock_index = MagicMock()
        mock_index.mark_deleted.side_effect = RuntimeError(
            "The requested to delete element is already deleted"
        )

        id_to_label = {"point_xyz": 3}
        label_to_id = {3: "point_xyz"}
        vector = np.random.randn(64).astype(np.float32)

        label, _, _, _ = manager.add_or_update_vector(
            mock_index, "point_xyz", vector, id_to_label, label_to_id, next_label=10
        )

        mock_index.add_items.assert_called_once()
        assert label == 3, "Returned label must match the pre-existing label"


class TestSaveIncrementalUpdateAtomic:
    """save_incremental_update() must write files atomically."""

    def test_save_incremental_update_no_tmp_files_remain(self, tmp_path: Path):
        """After a successful save_incremental_update(), no .tmp files remain."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()

        meta_file = collection_path / "collection_meta.json"
        with open(meta_file, "w") as f:
            json.dump({"name": "test"}, f)

        mock_index = _make_persisting_mock_index(current_count=2)
        id_to_label = {"p1": 0, "p2": 1}
        label_to_id = {0: "p1", 1: "p2"}

        manager.save_incremental_update(
            mock_index, collection_path, id_to_label, label_to_id, vector_count=2
        )

        tmp_files = list(collection_path.glob("*.tmp"))
        assert tmp_files == [], (
            f"No .tmp files should remain after save_incremental_update(), found: {tmp_files}"
        )

    def test_save_incremental_update_crash_leaves_metadata_consistent(
        self, tmp_path: Path
    ):
        """Original metadata survives a crash between HNSW write and metadata rename.

        With atomic temp-file + os.replace(), the original meta file is never
        overwritten until the rename succeeds.
        """
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()

        original_meta = {"name": "test", "original_key": "original_value"}
        meta_file = collection_path / "collection_meta.json"
        with open(meta_file, "w") as f:
            json.dump(original_meta, f)

        mock_index = _make_persisting_mock_index(current_count=1)
        id_to_label = {"p1": 0}
        label_to_id = {0: "p1"}

        original_json_dump = json.dump
        call_count = [0]

        def crashing_json_dump(obj, fp, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("simulated disk full during metadata write")
            return original_json_dump(obj, fp, **kwargs)

        with pytest.raises(OSError, match="simulated disk full"):
            with patch("json.dump", side_effect=crashing_json_dump):
                manager.save_incremental_update(
                    mock_index, collection_path, id_to_label, label_to_id, vector_count=1
                )

        with open(meta_file) as f:
            surviving_meta = json.load(f)

        assert surviving_meta == original_meta, (
            f"Metadata was corrupted by the crash. Got: {surviving_meta}, expected: {original_meta}"
        )
