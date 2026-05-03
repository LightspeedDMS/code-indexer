"""Regression tests for Bug #962: HNSW double-delete crash on reload-cycle.

HNSWIndexManager.remove_vector raises RuntimeError: "The requested to delete
element is already deleted" when temporal_indexer.close() runs
_apply_incremental_hnsw_batch_update on a point whose label has already been
soft-deleted in the on-disk HNSW index.

The same hazard exists in add_or_update_vector which also calls mark_deleted
before re-adding.

Root cause: load_for_incremental_update inverts the persisted label_to_id
mapping but carries NO information about which labels hnswlib has already
soft-deleted. If a previous batch deleted a label without evicting it from
label_to_id, the next batch reloads the stale mapping and mark_deleted raises.

Fix (Option 1 - minimal): wrap index.mark_deleted(label) in a narrow
try/except RuntimeError in BOTH remove_vector and add_or_update_vector.
Only catch RuntimeErrors whose message contains "already deleted".

This file specifically covers:
- remove_vector() does NOT raise on already-deleted error
- add_or_update_vector() does NOT raise on already-deleted error (update path)
- remove_vector() re-raises unrelated RuntimeErrors
- add_or_update_vector() re-raises unrelated RuntimeErrors
- delete -> reload -> delete-again sequence (the exact Bug #962 stack trace path)

The real hnswlib error message (hnswalg.h line ~884) is:
    "The requested to delete element is already deleted"
All tests use this exact message to guard against substring mismatch.
"""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

_ALREADY_DELETED_MSG = "The requested to delete element is already deleted"
_UNRELATED_ERROR_MSG = "some unrelated hnswlib internal error"


def _make_manager(vector_dim: int = 64) -> HNSWIndexManager:
    return HNSWIndexManager(vector_dim=vector_dim, space="cosine")


def _make_mock_index() -> MagicMock:
    """Return a fresh mock hnswlib.Index with no side-effects configured."""
    mock_index = MagicMock()
    mock_index.get_current_count.return_value = 10
    return mock_index


def _write_metadata(collection_path: Path, id_mapping: dict) -> None:
    """Write minimal collection_meta.json so _load_id_mapping() can read it."""
    meta = {
        "hnsw_index": {
            "id_mapping": {str(k): v for k, v in id_mapping.items()},
        }
    }
    meta_file = collection_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f)


class TestRemoveVectorAlreadyDeletedBug962:
    """remove_vector() must not raise when hnswlib reports already-deleted."""

    def test_remove_vector_survives_already_deleted_error(self):
        """remove_vector() does not raise when mark_deleted raises the real hnswlib error.

        Regression guard: temporal_indexer.close() -> end_indexing ->
        _apply_incremental_hnsw_batch_update -> remove_vector must not crash.
        """
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        id_to_label = {"point_abc": 7}
        label_to_id = {7: "point_abc"}

        # Must not raise
        manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)

        # Mapping cleanup must still happen even after swallowed exception
        assert "point_abc" not in id_to_label
        assert 7 not in label_to_id

    def test_remove_vector_cleans_mappings_on_already_deleted(self):
        """Mappings are evicted even when mark_deleted raises already-deleted."""
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        id_to_label = {"p1": 0, "p2": 1}
        label_to_id = {0: "p1", 1: "p2"}

        manager.remove_vector(mock_index, "p1", id_to_label, label_to_id)

        assert "p1" not in id_to_label
        assert 0 not in label_to_id
        # Sibling point untouched
        assert "p2" in id_to_label
        assert 1 in label_to_id

    def test_remove_vector_reraises_unrelated_runtime_error(self):
        """remove_vector() re-raises RuntimeErrors whose message does not contain 'already deleted'."""
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_UNRELATED_ERROR_MSG)

        id_to_label = {"point_abc": 5}
        label_to_id = {5: "point_abc"}

        with pytest.raises(RuntimeError, match=_UNRELATED_ERROR_MSG):
            manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)

    def test_remove_vector_noop_for_unknown_point(self):
        """remove_vector() silently ignores point_ids not in id_to_label."""
        manager = _make_manager()
        mock_index = _make_mock_index()

        id_to_label: dict = {}
        label_to_id: dict = {}

        # Must not raise and must not call mark_deleted
        manager.remove_vector(mock_index, "nonexistent_point", id_to_label, label_to_id)
        mock_index.mark_deleted.assert_not_called()


class TestAddOrUpdateVectorAlreadyDeletedBug962:
    """add_or_update_vector() must not raise when hnswlib reports already-deleted in update path."""

    def test_add_or_update_vector_survives_already_deleted_error(self):
        """add_or_update_vector() does not raise when mark_deleted raises already-deleted.

        The re-add (add_items) must still proceed after the swallowed exception.
        """
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        id_to_label = {"point_xyz": 3}
        label_to_id = {3: "point_xyz"}
        vector = np.random.randn(64).astype(np.float32)

        label, out_id_to_label, out_label_to_id, _ = manager.add_or_update_vector(
            mock_index, "point_xyz", vector, id_to_label, label_to_id, next_label=10
        )

        # Label must be the pre-existing one
        assert label == 3
        # Re-add must still have been called with correct label
        mock_index.add_items.assert_called_once()
        call_args = mock_index.add_items.call_args
        np.testing.assert_array_equal(call_args[0][1], np.array([3]))

    def test_add_or_update_vector_reraises_unrelated_runtime_error(self):
        """add_or_update_vector() re-raises RuntimeErrors unrelated to already-deleted."""
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_UNRELATED_ERROR_MSG)

        id_to_label = {"point_xyz": 3}
        label_to_id = {3: "point_xyz"}
        vector = np.random.randn(64).astype(np.float32)

        with pytest.raises(RuntimeError, match=_UNRELATED_ERROR_MSG):
            manager.add_or_update_vector(
                mock_index, "point_xyz", vector, id_to_label, label_to_id, next_label=10
            )

        # add_items must NOT be called when an unrelated error propagates
        mock_index.add_items.assert_not_called()

    def test_add_or_update_new_point_does_not_call_mark_deleted(self):
        """add_or_update_vector() with a new point_id skips mark_deleted entirely."""
        manager = _make_manager()
        mock_index = _make_mock_index()

        id_to_label: dict = {}
        label_to_id: dict = {}
        vector = np.random.randn(64).astype(np.float32)

        label, out_id_to_label, out_label_to_id, next_label = (
            manager.add_or_update_vector(
                mock_index, "new_point", vector, id_to_label, label_to_id, next_label=0
            )
        )

        mock_index.mark_deleted.assert_not_called()
        assert label == 0
        assert next_label == 1
        assert out_id_to_label["new_point"] == 0
        assert out_label_to_id[0] == "new_point"


class TestReloadCycleDoubleDeleteBug962:
    """Regression tests for the exact Bug #962 stack trace: delete -> reload -> delete-again.

    This is the specific failure mode reported:
    temporal_indexer.close() runs _apply_incremental_hnsw_batch_update on a point
    whose label has already been soft-deleted in the on-disk HNSW index from a
    previous batch pass. load_for_incremental_update rebuilds id_to_label from
    label_to_id metadata, which carries no soft-delete state, so the second call
    to mark_deleted raises RuntimeError.
    """

    def test_delete_reload_delete_again_sequence_does_not_raise(self, tmp_path: Path):
        """Simulates the reload-cycle double-delete scenario from the Bug #962 stack trace.

        Sequence:
        1. First batch: mark point deleted (succeeds)
        2. Reload id_to_label from stale metadata (point still present in mapping)
        3. Second batch: mark same point deleted again -> must not raise
        """
        manager = _make_manager()
        collection_path = tmp_path / "test_collection"
        collection_path.mkdir()

        # Stale metadata: point_abc is still in label_to_id (not evicted)
        # This simulates a reload after a previous batch soft-deleted it
        _write_metadata(collection_path, {5: "point_abc"})

        # Reload id_to_label from stale metadata (exactly as load_for_incremental_update does)
        label_to_id = manager._load_id_mapping(collection_path)
        id_to_label = {v: k for k, v in label_to_id.items()}

        assert "point_abc" in id_to_label, "Stale mapping must contain point_abc"
        assert id_to_label["point_abc"] == 5

        # Second batch: hnswlib already has label 5 soft-deleted
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        # Must not raise — this was the Bug #962 crash
        manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)

        # Mapping must be cleaned up so further operations are consistent
        assert "point_abc" not in id_to_label
        assert 5 not in label_to_id

    def test_delete_reload_update_already_deleted_does_not_raise(self, tmp_path: Path):
        """Simulates reload-cycle where an update path triggers the already-deleted error.

        Sequence:
        1. First batch: soft-deleted label 3 for point_xyz
        2. Reload stale mapping (point_xyz still present)
        3. Second batch: add_or_update_vector tries mark_deleted(3) again -> must not raise
        4. Re-add (add_items) still proceeds
        """
        manager = _make_manager()
        collection_path = tmp_path / "test_collection"
        collection_path.mkdir()

        _write_metadata(collection_path, {3: "point_xyz"})

        label_to_id = manager._load_id_mapping(collection_path)
        id_to_label = {v: k for k, v in label_to_id.items()}

        assert "point_xyz" in id_to_label

        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        vector = np.random.randn(64).astype(np.float32)

        # Must not raise
        label, _, _, _ = manager.add_or_update_vector(
            mock_index, "point_xyz", vector, id_to_label, label_to_id, next_label=10
        )

        assert label == 3
        # add_items must still have been called (re-add proceeds despite swallowed error)
        mock_index.add_items.assert_called_once()

    def test_warning_log_emitted_on_already_deleted_in_remove_vector(self, caplog):
        """A WARNING-level log is emitted when already-deleted is swallowed in remove_vector."""
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        id_to_label = {"point_abc": 7}
        label_to_id = {7: "point_abc"}

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.storage.hnsw_index_manager"
        ):
            manager.remove_vector(mock_index, "point_abc", id_to_label, label_to_id)

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("point_abc" in m or "7" in m for m in warning_messages), (
            f"Expected WARNING mentioning point or label, got: {warning_messages}"
        )

    def test_warning_log_emitted_on_already_deleted_in_add_or_update_vector(self, caplog):
        """A WARNING-level log is emitted when already-deleted is swallowed in add_or_update_vector."""
        manager = _make_manager()
        mock_index = _make_mock_index()
        mock_index.mark_deleted.side_effect = RuntimeError(_ALREADY_DELETED_MSG)

        id_to_label = {"point_xyz": 3}
        label_to_id = {3: "point_xyz"}
        vector = np.random.randn(64).astype(np.float32)

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.storage.hnsw_index_manager"
        ):
            manager.add_or_update_vector(
                mock_index, "point_xyz", vector, id_to_label, label_to_id, next_label=10
            )

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("point_xyz" in m or "3" in m for m in warning_messages), (
            f"Expected WARNING mentioning point or label, got: {warning_messages}"
        )
