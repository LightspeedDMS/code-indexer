"""Tests for Bug #954 / Bug #948: knn_query retry on contiguous-2D-array RuntimeError.

When hnswlib raises:
    RuntimeError: Cannot return the results in a contiguous 2D array.
    Probably ef or M is too small

the query() method must retry with progressively smaller k values:
    k_actual -> k_actual // 2 -> max(1, k_actual // 4) -> 1

A WARNING must be logged on the FIRST retry only (not on every attempt).
If ALL retries fail (including k=1), the original exception must be re-raised.
An unrelated RuntimeError must propagate immediately without retries.
"""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

_CONTIGUOUS_MSG = "Cannot return the results in a contiguous 2D array. Probably ef or M is too small"


def _make_manager(vector_dim: int = 64) -> HNSWIndexManager:
    return HNSWIndexManager(vector_dim=vector_dim, space="cosine")


def _make_mock_index(
    *,
    current_count: int,
    id_mapping: dict,
) -> MagicMock:
    """Build a mock hnswlib.Index with the given count and fixed knn_query result."""
    mock = MagicMock()
    mock.get_current_count.return_value = current_count
    return mock


def _write_metadata(collection_path: Path, id_mapping: dict) -> None:
    """Write minimal collection_meta.json with id_mapping for _load_id_mapping()."""
    meta = {
        "hnsw_index": {
            "id_mapping": {str(k): v for k, v in id_mapping.items()},
        }
    }
    meta_file = collection_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f)


class TestKnnQueryRetryOnContiguous2DArrayError:
    """query() must retry with smaller k when hnswlib raises the contiguous-2D-array error."""

    def test_retry_succeeds_on_second_attempt_with_half_k(self, tmp_path: Path):
        """knn_query fails at k_actual, succeeds at k_actual // 2 — returns results."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        # 10 vectors in metadata (k=10 initially)
        id_mapping = {i: f"vec_{i}" for i in range(10)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=10, id_mapping=id_mapping)

        # First call (k=10) raises; second call (k=5) succeeds
        success_labels = np.array([[0, 1, 2, 3, 4]])
        success_distances = np.array([[0.1, 0.2, 0.3, 0.4, 0.5]])

        call_count = [0]

        def knn_side_effect(query_vector, k):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError(_CONTIGUOUS_MSG)
            return success_labels, success_distances

        mock_index.knn_query.side_effect = knn_side_effect

        query_vector = np.random.randn(64).astype(np.float32)
        result_ids, result_distances = manager.query(
            mock_index, query_vector, collection_path, k=10, ef=50
        )

        assert len(result_ids) == 5
        assert len(result_distances) == 5
        assert call_count[0] == 2

    def test_retry_logs_warning_on_first_retry_only(self, tmp_path: Path, caplog):
        """A WARNING is emitted on the first retry, not on every attempt."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        id_mapping = {i: f"vec_{i}" for i in range(8)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=8, id_mapping=id_mapping)

        # First call fails, second succeeds
        success_labels = np.array([[0, 1]])
        success_distances = np.array([[0.1, 0.2]])
        call_count = [0]

        def knn_side_effect(query_vector, k):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError(_CONTIGUOUS_MSG)
            return success_labels, success_distances

        mock_index.knn_query.side_effect = knn_side_effect

        query_vector = np.random.randn(64).astype(np.float32)
        with caplog.at_level(logging.WARNING, logger="code_indexer.storage.hnsw_index_manager"):
            manager.query(mock_index, query_vector, collection_path, k=8, ef=50)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 WARNING on first retry, got {len(warning_records)}: "
            + ", ".join(r.message for r in warning_records)
        )
        assert "contiguous" in warning_records[0].message.lower() or "degraded" in warning_records[0].message.lower()

    def test_all_retries_fail_reraises_original_error(self, tmp_path: Path):
        """When all retry attempts fail (including k=1), the original error is re-raised."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        id_mapping = {i: f"vec_{i}" for i in range(4)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=4, id_mapping=id_mapping)

        # All knn_query calls raise the contiguous error
        mock_index.knn_query.side_effect = RuntimeError(_CONTIGUOUS_MSG)

        query_vector = np.random.randn(64).astype(np.float32)
        with pytest.raises(RuntimeError, match="contiguous 2D array"):
            manager.query(mock_index, query_vector, collection_path, k=4, ef=50)

    def test_unrelated_runtime_error_propagates_immediately(self, tmp_path: Path):
        """RuntimeErrors unrelated to 'contiguous 2D array' propagate without retry."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        id_mapping = {i: f"vec_{i}" for i in range(4)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=4, id_mapping=id_mapping)

        call_count = [0]

        def knn_side_effect(query_vector, k):
            call_count[0] += 1
            raise RuntimeError("some unrelated hnswlib internal error")

        mock_index.knn_query.side_effect = knn_side_effect

        query_vector = np.random.randn(64).astype(np.float32)
        with pytest.raises(RuntimeError, match="unrelated hnswlib"):
            manager.query(mock_index, query_vector, collection_path, k=4, ef=50)

        # Must not retry — only one call
        assert call_count[0] == 1

    def test_retry_progression_k_actual_half_quarter_one(self, tmp_path: Path):
        """Retries use k_actual, k_actual//2, max(1, k_actual//4), 1 in order."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        # 20 vectors so k_actual=20, half=10, quarter=5, last=1
        id_mapping = {i: f"vec_{i}" for i in range(20)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=20, id_mapping=id_mapping)

        attempted_ks = []

        def knn_side_effect(query_vector, k):
            attempted_ks.append(k)
            if k > 1:
                raise RuntimeError(_CONTIGUOUS_MSG)
            # Succeed at k=1
            return np.array([[0]]), np.array([[0.1]])

        mock_index.knn_query.side_effect = knn_side_effect

        query_vector = np.random.randn(64).astype(np.float32)
        manager.query(mock_index, query_vector, collection_path, k=20, ef=50)

        # k_actual=20, then 10 (20//2), then 5 (max(1,20//4)), then 1
        assert attempted_ks == [20, 10, 5, 1], (
            f"Expected retry sequence [20, 10, 5, 1], got {attempted_ks}"
        )

    def test_warning_logged_once_across_multiple_retries(self, tmp_path: Path, caplog):
        """Warning is logged exactly once even when multiple retries are needed."""
        manager = _make_manager(vector_dim=64)
        collection_path = tmp_path / "coll"
        collection_path.mkdir()

        id_mapping = {i: f"vec_{i}" for i in range(20)}
        _write_metadata(collection_path, id_mapping)

        mock_index = _make_mock_index(current_count=20, id_mapping=id_mapping)

        call_count = [0]

        def knn_side_effect(query_vector, k):
            call_count[0] += 1
            if call_count[0] < 4:
                raise RuntimeError(_CONTIGUOUS_MSG)
            # Succeed on the 4th call (k=1)
            return np.array([[0]]), np.array([[0.1]])

        mock_index.knn_query.side_effect = knn_side_effect

        query_vector = np.random.randn(64).astype(np.float32)
        with caplog.at_level(logging.WARNING, logger="code_indexer.storage.hnsw_index_manager"):
            manager.query(mock_index, query_vector, collection_path, k=20, ef=50)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 WARNING across all retries, got {len(warning_records)}"
        )
