"""
TDD tests for bug #941 — Branch isolation _batch_update_points triggers false
orphan deletion of sibling chunks (Vector file not found warnings).

Red phase: All tests must FAIL before implementation.

Four acceptance criteria:
  AC1 - _batch_update_payload_only does NOT trigger orphan cleanup for sibling chunks.
  AC2 - _batch_hide_files_in_branch routes through _batch_update_payload_only, not
        _batch_update_points.
  AC3 - end_indexing skips incremental HNSW update when _branch_isolation_did_filtered_rebuild
        is True (and resets the flag).
  AC4 - _apply_incremental_hnsw_batch_update filters out points present in both
        changes["added"] (or "updated") AND changes["deleted"], emitting no
        "Vector file not found" warning for those points.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level constants — no magic numbers in test bodies
# ---------------------------------------------------------------------------
TEST_VECTOR_DIM = 16
TEST_CHUNK_SIZE = 1000
TEST_CHUNK_OVERLAP = 100
TEST_MAX_FILE_SIZE = 1_000_000
TEST_PROCESSOR_VECTOR_SIZE = 768
TEST_ROLLING_WINDOW_SECONDS = 30.0
TEST_MIN_TIME_DIFF = 0.1


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _write_vector_file(
    collection_path: Path,
    point_id: str,
    file_path: str,
    vector_dim: int = TEST_VECTOR_DIM,
) -> Path:
    """Write a minimal vector JSON file and return its path."""
    vec_dir = collection_path / "vectors"
    vec_dir.mkdir(parents=True, exist_ok=True)
    dest = vec_dir / f"vector_{point_id}.json"
    data = {
        "id": point_id,
        "vector": [0.1] * vector_dim,
        "chunk_text": f"chunk for {point_id}",
        "payload": {
            "path": file_path,
            "type": "content",
            "hidden_branches": [],
        },
    }
    with open(dest, "w") as f:
        json.dump(data, f)
    return dest


def _seed_id_index(
    store: Any, collection_name: str, point_id: str, vector_file: Path
) -> None:
    """Inject an entry directly into the store's in-memory id_index."""
    with store._id_index_lock:
        if collection_name not in store._id_index:
            store._id_index[collection_name] = {}
        store._id_index[collection_name][point_id] = vector_file


def _make_processor(tmp_path: Path) -> Any:
    """Create a HighThroughputProcessor with mocked dependencies for unit testing."""
    from code_indexer.services.high_throughput_processor import HighThroughputProcessor
    from code_indexer.config import Config

    mock_config = Mock(spec=Config)
    mock_config.codebase_dir = tmp_path
    mock_config.exclude_dirs = []
    mock_config.exclude_files = []
    mock_config.file_extensions = ["py"]
    mock_config.project_root = tmp_path

    indexing_config = Mock()
    indexing_config.chunk_size = TEST_CHUNK_SIZE
    indexing_config.chunk_overlap = TEST_CHUNK_OVERLAP
    indexing_config.max_file_size = TEST_MAX_FILE_SIZE
    mock_config.indexing = indexing_config

    mock_config.filesystem = Mock()
    mock_config.filesystem.api_key = None
    mock_config.filesystem.vector_size = TEST_PROCESSOR_VECTOR_SIZE
    mock_config.collection_base_name = "test_collection"

    mock_vector_store = Mock()
    mock_vector_store.ensure_provider_aware_collection = Mock(
        return_value="test_collection"
    )
    mock_vector_store.resolve_collection_name = Mock(return_value="test_collection")
    mock_vector_store.scroll_points = Mock(return_value=([], None))
    mock_vector_store._batch_update_payload_only = Mock(return_value=True)
    mock_vector_store._batch_update_points = Mock(return_value=True)

    mock_provider = Mock()
    mock_provider.get_embeddings_batch = Mock(return_value=[])

    processor = HighThroughputProcessor.__new__(HighThroughputProcessor)
    processor.cancelled = False
    processor.progress_log = None
    processor._visibility_lock = threading.Lock()
    processor._git_lock = threading.Lock()
    processor._content_id_lock = threading.Lock()
    processor._database_lock = threading.Lock()
    processor._cancellation_event = threading.Event()
    processor._cancellation_lock = threading.Lock()
    processor._file_rate_lock = threading.Lock()
    processor._file_processing_start_time = None
    processor._file_completion_history = []
    processor._rolling_window_seconds = TEST_ROLLING_WINDOW_SECONDS
    processor._min_time_diff = TEST_MIN_TIME_DIFF
    processor._source_bytes_lock = threading.Lock()
    processor._total_source_bytes_processed = 0
    processor._source_bytes_history = []
    processor.config = mock_config
    processor.vector_store_client = mock_vector_store
    processor.embedding_provider = mock_provider
    return processor


# ---------------------------------------------------------------------------
# AC1 — _batch_update_payload_only does NOT trigger orphan cleanup
# ---------------------------------------------------------------------------


class TestBatchUpdatePayloadOnlyNoOrphanCleanup:
    """
    AC1: _batch_update_payload_only must NOT run orphan-cleanup for sibling chunks.

    When a file has 3 chunks (chunk_a, chunk_b, chunk_c) and only chunk_a is
    targeted by a payload-only update, chunk_b and chunk_c must remain in the
    id_index and on disk after the call.
    """

    def test_batch_update_payload_only_does_not_trigger_orphan_cleanup(
        self, tmp_path: Path
    ):
        """
        GIVEN a collection with a 3-chunk file (chunk_a, chunk_b, chunk_c)
        WHEN _batch_update_payload_only is called updating only chunk_a's payload
        THEN chunk_b and chunk_c still exist in _id_index
        AND chunk_b and chunk_c vector files still exist on disk
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        collection_name = "test_coll"
        store.create_collection(collection_name, vector_size=TEST_VECTOR_DIM)
        collection_path = tmp_path / collection_name
        file_path = "src/large_file.py"

        # Write 3 sibling chunks for the same file
        file_a = _write_vector_file(collection_path, "chunk_a", file_path)
        file_b = _write_vector_file(collection_path, "chunk_b", file_path)
        file_c = _write_vector_file(collection_path, "chunk_c", file_path)

        # Seed the id_index so the store knows about all 3 chunks
        _seed_id_index(store, collection_name, "chunk_a", file_a)
        _seed_id_index(store, collection_name, "chunk_b", file_b)
        _seed_id_index(store, collection_name, "chunk_c", file_c)

        # Perform a payload-only update targeting only chunk_a
        points = [{"id": "chunk_a", "payload": {"hidden_branches": ["feature-x"]}}]
        result = store._batch_update_payload_only(points, collection_name)

        assert result is True

        # chunk_b and chunk_c must still be in the id_index
        with store._id_index_lock:
            id_index = store._id_index.get(collection_name, {})
        assert "chunk_b" in id_index, "chunk_b was removed from id_index (false orphan)"
        assert "chunk_c" in id_index, "chunk_c was removed from id_index (false orphan)"

        # chunk_b and chunk_c vector files must still exist on disk
        assert file_b.exists(), "chunk_b vector file was deleted (false orphan)"
        assert file_c.exists(), "chunk_c vector file was deleted (false orphan)"


# ---------------------------------------------------------------------------
# AC2 — _batch_hide_files_in_branch uses _batch_update_payload_only
# ---------------------------------------------------------------------------


class TestBatchHideFilesInBranchUsesPayloadOnlyPath:
    """
    AC2: _batch_hide_files_in_branch must call _batch_update_payload_only,
    NOT _batch_update_points.

    This mirrors the fix already applied to the symmetric "ensure visible" path
    (_batch_ensure_files_visible_in_branch uses _batch_update_payload_only at
    high_throughput_processor.py:1333). The "hide" path must be symmetric.
    """

    def test_batch_hide_files_in_branch_uses_payload_only_path(self, tmp_path: Path):
        """
        GIVEN a processor with mocked vector_store_client
        WHEN _batch_hide_files_in_branch is called with points that need hiding
        THEN _batch_update_payload_only is called (not _batch_update_points)
        AND the update includes the branch in hidden_branches for the targeted point
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            {
                "id": "point_001",
                "payload": {
                    "type": "content",
                    "path": "src/file.py",
                    "hidden_branches": [],
                },
            }
        ]

        processor._batch_hide_files_in_branch(
            file_paths=["src/file.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        # Must have used the payload-only path
        processor.vector_store_client._batch_update_payload_only.assert_called_once()

        # Must NOT have used the heavy upsert path
        processor.vector_store_client._batch_update_points.assert_not_called()

        # The update payload must include feature-x in hidden_branches
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates: List[Dict[str, Any]] = call_args[0][0]
        assert len(updates) == 1
        assert updates[0]["id"] == "point_001"
        assert "feature-x" in updates[0]["payload"]["hidden_branches"]


# ---------------------------------------------------------------------------
# AC3 — end_indexing skips incremental when filtered-rebuild flag is set
# ---------------------------------------------------------------------------


class TestEndIndexingSkipsIncrementalWhenFilteredRebuildFlagSet:
    """
    AC3: When _branch_isolation_did_filtered_rebuild is True before end_indexing,
    the incremental HNSW update path must NOT be entered, and the flag must be
    reset to False afterward.
    """

    def test_end_indexing_skips_incremental_when_filtered_rebuild_flag_set(
        self, tmp_path: Path
    ):
        """
        GIVEN a collection with begin_indexing called and some changes tracked
        AND _branch_isolation_did_filtered_rebuild is set to True
        WHEN end_indexing is called
        THEN _apply_incremental_hnsw_batch_update is NOT called
        AND _branch_isolation_did_filtered_rebuild is reset to False
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        collection_name = "test_coll"
        store.create_collection(collection_name, vector_size=TEST_VECTOR_DIM)

        # Simulate a session with some changes tracked
        store.begin_indexing(collection_name)
        store._indexing_session_changes[collection_name]["added"].add("some_point_id")

        # Flag indicating a filtered HNSW rebuild already happened during branch isolation
        store._branch_isolation_did_filtered_rebuild = True

        with patch.object(
            store,
            "_apply_incremental_hnsw_batch_update",
            wraps=store._apply_incremental_hnsw_batch_update,
        ) as mock_incremental:
            store.end_indexing(collection_name)

        # Incremental update must NOT have been called
        mock_incremental.assert_not_called()

        # Flag must have been reset
        assert store._branch_isolation_did_filtered_rebuild is False


# ---------------------------------------------------------------------------
# AC4 — _apply_incremental_hnsw_batch_update filters added-then-deleted points
# ---------------------------------------------------------------------------


class TestApplyIncrementalFiltersAddedMinusDeleted:
    """
    AC4: When a point appears in both changes["added"] (or "updated") AND
    changes["deleted"], _apply_incremental_hnsw_batch_update must treat it as
    a no-op and NOT emit a "Vector file not found" warning.
    """

    def test_apply_incremental_filters_added_minus_deleted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """
        GIVEN a collection with an existing HNSW index built from a real prior pass
        AND changes["added"] = {"ghost_point"} AND changes["deleted"] = {"ghost_point"}
        (i.e., the point was added then deleted in the same session)
        WHEN end_indexing is called (which drives _apply_incremental_hnsw_batch_update)
        THEN no "Vector file not found" warning is emitted for ghost_point
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        collection_name = "test_coll"
        store.create_collection(collection_name, vector_size=TEST_VECTOR_DIM)
        collection_path = tmp_path / collection_name

        # Insert one real point and build a proper HNSW index via end_indexing
        store.begin_indexing(collection_name)
        real_point = {
            "id": "real_point",
            "vector": [0.1] * TEST_VECTOR_DIM,
            "chunk_text": "real content",
            "payload": {"path": "src/real.py", "type": "content"},
        }
        store.upsert_points(collection_name, [real_point])
        store.end_indexing(collection_name)

        # HNSW index must exist for the incremental path to activate
        hnsw_file = collection_path / "hnsw_index.bin"
        assert hnsw_file.exists(), (
            "HNSW index must exist for incremental path to activate"
        )

        # New session: ghost_point appears in both added and deleted
        store.begin_indexing(collection_name)
        ghost_id = "ghost_point"
        store._indexing_session_changes[collection_name]["added"].add(ghost_id)
        store._indexing_session_changes[collection_name]["deleted"].add(ghost_id)
        # ghost_point has NO vector file on disk and is NOT in id_index

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.storage.filesystem_vector_store",
        ):
            store.end_indexing(collection_name)

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        ghost_warnings = [
            m
            for m in warning_messages
            if ghost_id in m and "Vector file not found" in m
        ]
        assert ghost_warnings == [], (
            f"Expected no 'Vector file not found' warnings for '{ghost_id}', "
            f"but got: {ghost_warnings}"
        )
