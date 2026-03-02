"""
TDD tests for batch payload update optimization (Story #339).

Fix B: _batch_update_payload_only - Lightweight payload-only JSON update
Fix C: scroll_points _parse_filter hoisting - Parse filter once, not per-file

These tests are written FIRST (TDD red phase). Implementation follows.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch
from typing import List


class TestBatchUpdatePayloadOnly:
    """
    Tests for Fix B: FilesystemVectorStore._batch_update_payload_only

    Acceptance criteria:
    - Merges only specified payload fields, preserves all other payload keys
    - Preserves vector data and chunk_text in JSON file untouched
    - Skips points whose id is not in id_index (graceful no-op)
    - Handles empty points list without error
    - Returns True on success
    """

    def _write_vector_file(self, collection_path: Path, point_id: str, data: dict) -> Path:
        """Write a vector JSON file directly to a flat location for test setup."""
        vector_file = collection_path / f"vector_{point_id}.json"
        with open(vector_file, "w") as f:
            json.dump(data, f)
        return vector_file

    def _seed_id_index(self, store, collection_name: str, point_id: str, vector_file: Path):
        """Inject an entry into the store's id_index directly."""
        with store._id_index_lock:
            if collection_name not in store._id_index:
                store._id_index[collection_name] = {}
            store._id_index[collection_name][point_id] = vector_file

    def test_batch_update_payload_only_merges_payload_fields(self, tmp_path):
        """
        SCENARIO: Only specified payload fields are merged; others are preserved.

        GIVEN a vector JSON file with payload {type: content, path: a.py, language: python, hidden_branches: [main]}
        WHEN _batch_update_payload_only is called with {id: X, payload: {hidden_branches: []}}
        THEN the JSON file has hidden_branches updated to []
        AND type, path, language are unchanged
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"

        original_data = {
            "id": "point_001",
            "vector": [0.1, 0.2, 0.3],
            "chunk_text": "def foo(): pass",
            "payload": {
                "type": "content",
                "path": "src/foo.py",
                "language": "python",
                "hidden_branches": ["main"],
            },
        }

        vector_file = self._write_vector_file(collection_path, "point_001", original_data)
        self._seed_id_index(store, "test_coll", "point_001", vector_file)

        points = [{"id": "point_001", "payload": {"hidden_branches": []}}]
        result = store._batch_update_payload_only(points, "test_coll")

        assert result is True

        with open(vector_file) as f:
            updated = json.load(f)

        assert updated["payload"]["hidden_branches"] == []
        assert updated["payload"]["type"] == "content"
        assert updated["payload"]["path"] == "src/foo.py"
        assert updated["payload"]["language"] == "python"

    def test_batch_update_payload_only_preserves_vector_and_chunk_text(self, tmp_path):
        """
        SCENARIO: Vector data and chunk_text are never modified.

        GIVEN a vector JSON file with vector=[1.0, 2.0] and chunk_text="hello"
        WHEN _batch_update_payload_only updates hidden_branches
        THEN vector is still [1.0, 2.0] and chunk_text is still "hello"
        AND no projection matrix is loaded (no upsert_points called)
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"

        original_vector = [1.1, 2.2, 3.3, 4.4]
        original_chunk_text = "def important_function(): return 42"

        original_data = {
            "id": "point_002",
            "vector": original_vector,
            "chunk_text": original_chunk_text,
            "payload": {
                "type": "content",
                "path": "src/bar.py",
                "hidden_branches": ["feature-x"],
            },
        }

        vector_file = self._write_vector_file(collection_path, "point_002", original_data)
        self._seed_id_index(store, "test_coll", "point_002", vector_file)

        # Patch upsert_points to verify it is NOT called
        with patch.object(store, "upsert_points") as mock_upsert:
            points = [{"id": "point_002", "payload": {"hidden_branches": []}}]
            result = store._batch_update_payload_only(points, "test_coll")

            mock_upsert.assert_not_called()

        assert result is True

        with open(vector_file) as f:
            updated = json.load(f)

        assert updated["vector"] == original_vector
        assert updated["chunk_text"] == original_chunk_text

    def test_batch_update_payload_only_skips_missing_id_index_entry(self, tmp_path):
        """
        SCENARIO: Points not in id_index are silently skipped.

        GIVEN a point_id "ghost_point" not in the id_index
        WHEN _batch_update_payload_only is called with that point
        THEN it does not raise an exception
        AND returns True (graceful no-op)
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        # id_index is empty - ghost_point is not registered
        points = [{"id": "ghost_point", "payload": {"hidden_branches": []}}]
        result = store._batch_update_payload_only(points, "test_coll")

        assert result is True

    def test_batch_update_payload_only_handles_empty_list(self, tmp_path):
        """
        SCENARIO: Empty list is handled without errors.

        GIVEN an empty points list
        WHEN _batch_update_payload_only is called
        THEN no file I/O occurs and True is returned
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        result = store._batch_update_payload_only([], "test_coll")

        assert result is True

    def test_batch_update_payload_only_handles_multiple_points(self, tmp_path):
        """
        SCENARIO: Multiple points are all updated in a single call.

        GIVEN 3 vector files each with hidden_branches containing "feature-a"
        WHEN _batch_update_payload_only is called with all 3 points
        THEN all 3 files have hidden_branches updated correctly
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"
        vector_files = []

        for i in range(3):
            pid = f"point_{i:03d}"
            data = {
                "id": pid,
                "vector": [float(i)],
                "chunk_text": f"chunk {i}",
                "payload": {
                    "type": "content",
                    "path": f"src/file{i}.py",
                    "hidden_branches": ["feature-a"],
                },
            }
            vf = self._write_vector_file(collection_path, pid, data)
            self._seed_id_index(store, "test_coll", pid, vf)
            vector_files.append((pid, vf))

        points = [
            {"id": pid, "payload": {"hidden_branches": []}}
            for pid, _ in vector_files
        ]
        result = store._batch_update_payload_only(points, "test_coll")

        assert result is True
        for pid, vf in vector_files:
            with open(vf) as f:
                data = json.load(f)
            assert data["payload"]["hidden_branches"] == [], f"Point {pid} not updated"

    def test_batch_update_payload_only_skips_nonexistent_file(self, tmp_path):
        """
        SCENARIO: If id_index entry points to a deleted file, skip gracefully.

        GIVEN a point registered in id_index but the file has been deleted
        WHEN _batch_update_payload_only is called
        THEN it does not raise an exception and returns True
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        # Register a path that doesn't exist
        ghost_file = tmp_path / "test_coll" / "vector_ghost.json"
        self._seed_id_index(store, "test_coll", "ghost_id", ghost_file)

        points = [{"id": "ghost_id", "payload": {"hidden_branches": []}}]
        result = store._batch_update_payload_only(points, "test_coll")

        assert result is True


class TestScrollPointsFilterHoisting:
    """
    Tests for Fix C: scroll_points _parse_filter called once, not per-file.

    Acceptance criteria:
    - _parse_filter called exactly once regardless of number of files
    - Filter behavior unchanged (same results as before the optimization)
    - No filter_conditions case still works (filter_func is None)
    """

    def _create_vector_files(
        self, collection_path: Path, count: int, type_value: str = "content", prefix: str = "point"
    ) -> List[Path]:
        """Create multiple vector JSON files for testing."""
        files = []
        for i in range(count):
            pid = f"{prefix}_{i:05d}"
            data = {
                "id": pid,
                "vector": [float(i), float(i + 1)],
                "payload": {
                    "type": type_value,
                    "path": f"src/file{i}.py",
                    "language": "python",
                },
            }
            vf = collection_path / f"vector_{pid}.json"
            with open(vf, "w") as f:
                json.dump(data, f)
            files.append(vf)
        return files

    def test_parse_filter_called_once_not_per_file(self, tmp_path):
        """
        SCENARIO: _parse_filter is called exactly once per scroll_points invocation.

        GIVEN a collection with 50 vector files
        WHEN scroll_points is called with filter_conditions
        THEN _parse_filter is called exactly once (not 50 times)
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"
        self._create_vector_files(collection_path, 50)

        parse_filter_call_count = []
        original_parse_filter = store._parse_filter

        def counting_parse_filter(filter_conditions):
            parse_filter_call_count.append(1)
            return original_parse_filter(filter_conditions)

        filter_conditions = {"must": [{"key": "type", "match": {"value": "content"}}]}

        with patch.object(store, "_parse_filter", side_effect=counting_parse_filter):
            store.scroll_points(
                collection_name="test_coll",
                filter_conditions=filter_conditions,
                limit=1000,
            )

        # CRITICAL: _parse_filter must be called exactly ONCE, not 50 times
        assert len(parse_filter_call_count) == 1, (
            f"_parse_filter was called {len(parse_filter_call_count)} times, expected exactly 1. "
            "Fix C requires hoisting _parse_filter outside the per-file loop."
        )

    def test_filter_behavior_unchanged_after_hoisting(self, tmp_path):
        """
        SCENARIO: Same results before and after optimization.

        GIVEN a collection with 20 content files and 10 metadata files
        WHEN scroll_points filters for type=content
        THEN only content files are returned
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"
        self._create_vector_files(collection_path, 20, type_value="content", prefix="content")
        self._create_vector_files(collection_path, 10, type_value="metadata", prefix="meta")

        filter_conditions = {"must": [{"key": "type", "match": {"value": "content"}}]}
        points, _ = store.scroll_points(
            collection_name="test_coll",
            filter_conditions=filter_conditions,
            limit=1000,
        )

        # All 20 content points returned, 10 metadata excluded
        assert len(points) == 20, f"Expected 20 content points, got {len(points)}"
        for point in points:
            assert point["payload"]["type"] == "content"

    def test_no_filter_conditions_returns_all_files(self, tmp_path):
        """
        SCENARIO: When no filter_conditions, all files are returned (filter_func is None).

        GIVEN a collection with 15 vector files
        WHEN scroll_points is called without filter_conditions
        THEN all 15 points are returned
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"
        self._create_vector_files(collection_path, 15)

        points, _ = store.scroll_points(
            collection_name="test_coll",
            limit=1000,
        )

        assert len(points) == 15

    def test_parse_filter_with_no_filter_conditions_not_called(self, tmp_path):
        """
        SCENARIO: _parse_filter is NOT called when no filter_conditions provided.

        GIVEN a collection with 10 files
        WHEN scroll_points is called without filter_conditions
        THEN _parse_filter is never called
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("test_coll", vector_size=1536)

        collection_path = tmp_path / "test_coll"
        self._create_vector_files(collection_path, 10)

        with patch.object(store, "_parse_filter") as mock_parse:
            store.scroll_points(collection_name="test_coll", limit=1000)
            mock_parse.assert_not_called()
