"""
TDD tests for batch visibility update performance (Story #339).

Fix A: _batch_ensure_files_visible_in_branch - batch operation replacing per-file loop
Fix D: _fetch_all_content_points - paginated fetch helper with no silent truncation

These tests are written FIRST (TDD red phase). Implementation follows.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
from typing import List, Dict, Any


def _make_content_point(point_id: str, file_path: str, hidden_branches: List[str] = None) -> Dict[str, Any]:
    """Create a minimal content point dict matching what scroll_points returns."""
    return {
        "id": point_id,
        "payload": {
            "type": "content",
            "path": file_path,
            "hidden_branches": hidden_branches if hidden_branches is not None else [],
        },
    }


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
    indexing_config.chunk_size = 1000
    indexing_config.chunk_overlap = 100
    indexing_config.max_file_size = 1000000
    mock_config.indexing = indexing_config

    mock_config.filesystem = Mock()
    mock_config.filesystem.url = "http://localhost:6333"
    mock_config.filesystem.api_key = None
    mock_config.filesystem.vector_size = 768
    mock_config.collection_base_name = "test_collection"

    mock_vector_store = Mock()
    mock_vector_store.ensure_provider_aware_collection = Mock(return_value="test_collection")
    mock_vector_store.resolve_collection_name = Mock(return_value="test_collection")
    mock_vector_store.scroll_points = Mock(return_value=([], None))
    mock_vector_store._batch_update_payload_only = Mock(return_value=True)
    mock_vector_store._batch_update_points = Mock(return_value=True)

    mock_provider = Mock()
    mock_provider.get_embeddings_batch = Mock(return_value=[])

    processor = HighThroughputProcessor.__new__(HighThroughputProcessor)

    import threading
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
    processor._rolling_window_seconds = 30.0
    processor._min_time_diff = 0.1
    processor._source_bytes_lock = threading.Lock()
    processor._total_source_bytes_processed = 0
    processor._source_bytes_history = []
    processor.config = mock_config
    processor.vector_store_client = mock_vector_store
    processor.embedding_provider = mock_provider

    return processor


class TestBatchEnsureFilesVisibleInBranch:
    """
    Tests for Fix A: HighThroughputProcessor._batch_ensure_files_visible_in_branch

    Acceptance criteria:
    - Removes branch from hidden_branches for points whose path is in unchanged set
    - Does not modify points whose path is NOT in the unchanged set
    - Collects all updates and calls _batch_update_payload_only once (not per-point)
    - Acquires _visibility_lock once for entire batch (not per-file)
    - Handles empty file_paths list as no-op
    - Handles points with no hidden_branches key gracefully
    """

    def test_removes_branch_from_hidden_branches_for_matching_paths(self, tmp_path):
        """
        SCENARIO: Branch is removed from hidden_branches for paths in unchanged set.

        GIVEN all_content_points has 3 points for paths [a.py, b.py, c.py]
          AND all points have hidden_branches = ["feature-x"]
        WHEN _batch_ensure_files_visible_in_branch is called with [a.py, b.py, c.py], branch="feature-x"
        THEN _batch_update_payload_only is called with 3 updates
          AND each update has hidden_branches = []
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            _make_content_point("id_001", "src/a.py", ["feature-x"]),
            _make_content_point("id_002", "src/b.py", ["feature-x"]),
            _make_content_point("id_003", "src/c.py", ["feature-x"]),
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/a.py", "src/b.py", "src/c.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_called_once()
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates = call_args[0][0]

        assert len(updates) == 3
        point_ids = {u["id"] for u in updates}
        assert point_ids == {"id_001", "id_002", "id_003"}
        for update in updates:
            assert update["payload"]["hidden_branches"] == []

    def test_does_not_modify_points_not_in_unchanged_set(self, tmp_path):
        """
        SCENARIO: Points for paths NOT in unchanged_files are untouched.

        GIVEN all_content_points has points for [a.py, other.py]
          AND both have hidden_branches = ["feature-x"]
        WHEN _batch_ensure_files_visible_in_branch is called with [a.py] only
        THEN only a.py's point is included in updates
          AND other.py's point is NOT in updates
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            _make_content_point("id_001", "src/a.py", ["feature-x"]),
            _make_content_point("id_999", "src/other.py", ["feature-x"]),
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/a.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_called_once()
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates = call_args[0][0]

        assert len(updates) == 1
        assert updates[0]["id"] == "id_001"
        # other.py must NOT be present
        updated_ids = {u["id"] for u in updates}
        assert "id_999" not in updated_ids

    def test_calls_batch_update_exactly_once_not_per_point(self, tmp_path):
        """
        SCENARIO: _batch_update_payload_only is called once, not once per matching point.

        GIVEN all_content_points has 10 points all needing visibility update
        WHEN _batch_ensure_files_visible_in_branch is called
        THEN _batch_update_payload_only is called exactly once with all updates
        """
        processor = _make_processor(tmp_path)

        file_paths = [f"src/file{i}.py" for i in range(10)]
        all_content_points = [
            _make_content_point(f"id_{i:03d}", f"src/file{i}.py", ["branch-a"])
            for i in range(10)
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=file_paths,
            branch="branch-a",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        # CRITICAL: exactly ONE call, not 10
        assert processor.vector_store_client._batch_update_payload_only.call_count == 1

    def test_handles_empty_file_paths_as_noop(self, tmp_path):
        """
        SCENARIO: Empty file_paths results in no-op.

        GIVEN file_paths is empty
        WHEN _batch_ensure_files_visible_in_branch is called
        THEN _batch_update_payload_only is NOT called
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            _make_content_point("id_001", "src/a.py", ["main"]),
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=[],
            branch="main",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_not_called()

    def test_handles_points_with_no_hidden_branches_key(self, tmp_path):
        """
        SCENARIO: Points without hidden_branches key are handled gracefully.

        GIVEN a point with payload that has no hidden_branches key
        WHEN _batch_ensure_files_visible_in_branch is called
        THEN it does not raise KeyError
          AND no update is generated for that point (branch not in empty list)
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            {
                "id": "id_no_hidden",
                "payload": {
                    "type": "content",
                    "path": "src/a.py",
                    # No hidden_branches key
                },
            }
        ]

        # Should not raise
        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/a.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        # No update needed since hidden_branches defaults to [] and "feature-x" not in []
        processor.vector_store_client._batch_update_payload_only.assert_not_called()

    def test_skips_points_where_branch_not_in_hidden_branches(self, tmp_path):
        """
        SCENARIO: Points where branch is NOT in hidden_branches need no update.

        GIVEN a point with hidden_branches=["other-branch"] (not "feature-x")
        WHEN _batch_ensure_files_visible_in_branch is called for branch="feature-x"
        THEN no update is generated
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            _make_content_point("id_001", "src/a.py", ["other-branch"]),
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/a.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_not_called()

    def test_acquires_visibility_lock_once_not_per_file(self, tmp_path):
        """
        SCENARIO: _visibility_lock is acquired once for the entire batch.

        GIVEN 100 files all needing visibility update
        WHEN _batch_ensure_files_visible_in_branch is called
        THEN the lock is acquired exactly once (not 100 times)
        """
        processor = _make_processor(tmp_path)

        import threading
        lock_acquire_count = []
        original_lock = threading.Lock()
        real_acquire = original_lock.acquire

        class CountingLock:
            def __enter__(self_inner):
                lock_acquire_count.append(1)
                return self

            def __exit__(self_inner, *args):
                pass

        processor._visibility_lock = CountingLock()

        file_paths = [f"src/file{i}.py" for i in range(100)]
        all_content_points = [
            _make_content_point(f"id_{i:03d}", f"src/file{i}.py", ["branch-test"])
            for i in range(100)
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=file_paths,
            branch="branch-test",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        # Lock acquired exactly once for the entire batch, not per-file
        assert len(lock_acquire_count) == 1, (
            f"Lock acquired {len(lock_acquire_count)} times, expected exactly 1. "
            "Fix A requires acquiring lock once for entire batch."
        )

    def test_removes_only_target_branch_from_hidden_branches(self, tmp_path):
        """
        SCENARIO: Only the target branch is removed; other hidden branches are preserved.

        GIVEN a point with hidden_branches = ["main", "feature-x", "develop"]
        WHEN _batch_ensure_files_visible_in_branch is called for branch="feature-x"
        THEN hidden_branches becomes ["main", "develop"]
        """
        processor = _make_processor(tmp_path)

        all_content_points = [
            _make_content_point("id_001", "src/a.py", ["main", "feature-x", "develop"]),
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/a.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_called_once()
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates = call_args[0][0]
        assert len(updates) == 1
        # "feature-x" removed, "main" and "develop" preserved
        remaining = updates[0]["payload"]["hidden_branches"]
        assert "feature-x" not in remaining
        assert "main" in remaining
        assert "develop" in remaining


class TestFetchAllContentPoints:
    """
    Tests for Fix D: HighThroughputProcessor._fetch_all_content_points

    Acceptance criteria:
    - Returns all points when total exceeds single page limit
    - Handles single-page result (next_offset is None immediately)
    - Handles empty collection (returns empty list)
    - Breaks on stuck pagination (next_offset equals current offset)
    - Excludes vectors from fetched data (with_vectors=False)
    """

    def test_paginates_through_multiple_pages(self, tmp_path):
        """
        SCENARIO: All points returned when collection exceeds single page.

        GIVEN scroll_points returns page1 (5000 items, offset="page2_offset")
          AND then page2 (3000 items, offset=None)
        WHEN _fetch_all_content_points is called
        THEN returns 8000 items total
        AND scroll_points called twice
        """
        processor = _make_processor(tmp_path)

        page1_points = [_make_content_point(f"id_{i}", f"file{i}.py") for i in range(5000)]
        page2_points = [_make_content_point(f"id_{i}", f"file{i}.py") for i in range(5000, 8000)]

        scroll_responses = [
            (page1_points, "page2_offset"),
            (page2_points, None),
        ]
        response_iter = iter(scroll_responses)

        def mock_scroll(**kwargs):
            return next(response_iter)

        processor.vector_store_client.scroll_points = mock_scroll

        result = processor._fetch_all_content_points("test_collection")

        assert len(result) == 8000

    def test_handles_single_page_result(self, tmp_path):
        """
        SCENARIO: Single-page collection returns all items immediately.

        GIVEN scroll_points returns 100 items and next_offset=None
        WHEN _fetch_all_content_points is called
        THEN returns 100 items
        AND scroll_points called exactly once
        """
        processor = _make_processor(tmp_path)

        points = [_make_content_point(f"id_{i}", f"file{i}.py") for i in range(100)]
        processor.vector_store_client.scroll_points = Mock(return_value=(points, None))

        result = processor._fetch_all_content_points("test_collection")

        assert len(result) == 100
        assert processor.vector_store_client.scroll_points.call_count == 1

    def test_handles_empty_collection(self, tmp_path):
        """
        SCENARIO: Empty collection returns empty list.

        GIVEN scroll_points returns empty list and next_offset=None
        WHEN _fetch_all_content_points is called
        THEN returns empty list
        """
        processor = _make_processor(tmp_path)
        processor.vector_store_client.scroll_points = Mock(return_value=([], None))

        result = processor._fetch_all_content_points("test_collection")

        assert result == []

    def test_breaks_on_stuck_pagination(self, tmp_path):
        """
        SCENARIO: Safety check breaks infinite loop when pagination stalls.

        GIVEN scroll_points keeps returning the same offset ("stuck_offset")
        WHEN _fetch_all_content_points is called
        THEN it does not loop forever
        AND it eventually returns what was collected before detecting the stuck state
        """
        processor = _make_processor(tmp_path)

        page_points = [_make_content_point("id_0", "file0.py")]

        call_count = [0]

        def always_same_offset(**kwargs):
            call_count[0] += 1
            if call_count[0] > 10:
                # Safety: test should not call more than 10 times in stuck state
                raise RuntimeError("Pagination stuck protection in test: too many calls")
            offset = kwargs.get("offset")
            if offset == "stuck_offset":
                # Stuck: same offset returned again
                return page_points, "stuck_offset"
            # First call returns stuck_offset
            return page_points, "stuck_offset"

        processor.vector_store_client.scroll_points = always_same_offset

        # Should break out of the loop without raising
        result = processor._fetch_all_content_points("test_collection")

        # Should have collected the first page's data and broken out
        assert len(result) >= 1

    def test_calls_scroll_points_with_vectors_false(self, tmp_path):
        """
        SCENARIO: Vectors are excluded from fetch (memory optimization).

        GIVEN a collection with content points
        WHEN _fetch_all_content_points is called
        THEN scroll_points is called with with_vectors=False
        """
        processor = _make_processor(tmp_path)

        points = [_make_content_point(f"id_{i}", f"file{i}.py") for i in range(10)]
        processor.vector_store_client.scroll_points = Mock(return_value=(points, None))

        processor._fetch_all_content_points("test_collection")

        call_kwargs = processor.vector_store_client.scroll_points.call_args[1]
        assert call_kwargs.get("with_vectors") is False, (
            "with_vectors must be False to avoid loading large vector arrays into memory"
        )

    def test_calls_scroll_points_with_content_type_filter(self, tmp_path):
        """
        SCENARIO: Only content-type points are fetched (not metadata points).

        WHEN _fetch_all_content_points is called
        THEN scroll_points is called with filter for type=content
        """
        processor = _make_processor(tmp_path)
        processor.vector_store_client.scroll_points = Mock(return_value=([], None))

        processor._fetch_all_content_points("test_collection")

        call_kwargs = processor.vector_store_client.scroll_points.call_args[1]
        filter_cond = call_kwargs.get("filter_conditions", {})

        # Must have a "must" clause filtering for type=content
        assert "must" in filter_cond
        must_clauses = filter_cond["must"]
        found_type_filter = any(
            clause.get("key") == "type" and clause.get("match", {}).get("value") == "content"
            for clause in must_clauses
        )
        assert found_type_filter, (
            "filter_conditions must include type=content filter to exclude metadata points"
        )

    def test_calls_scroll_points_with_limit_5000(self, tmp_path):
        """
        SCENARIO: Pagination uses limit=5000 for efficiency.

        WHEN _fetch_all_content_points is called
        THEN scroll_points is called with limit=5000
        """
        processor = _make_processor(tmp_path)
        processor.vector_store_client.scroll_points = Mock(return_value=([], None))

        processor._fetch_all_content_points("test_collection")

        call_kwargs = processor.vector_store_client.scroll_points.call_args[1]
        assert call_kwargs.get("limit") == 5000, (
            f"Expected limit=5000, got limit={call_kwargs.get('limit')}. "
            "Larger page size reduces number of round trips."
        )

    def test_three_page_pagination_collects_all(self, tmp_path):
        """
        SCENARIO: Three-page pagination collects all items.

        GIVEN scroll_points returns 3 pages of 5000, 5000, 2000 items
        WHEN _fetch_all_content_points is called
        THEN returns 12000 items total
        """
        processor = _make_processor(tmp_path)

        page1 = [_make_content_point(f"id_{i}", f"f{i}.py") for i in range(5000)]
        page2 = [_make_content_point(f"id_{i}", f"f{i}.py") for i in range(5000, 10000)]
        page3 = [_make_content_point(f"id_{i}", f"f{i}.py") for i in range(10000, 12000)]

        responses = iter([
            (page1, "offset_page2"),
            (page2, "offset_page3"),
            (page3, None),
        ])

        processor.vector_store_client.scroll_points = Mock(side_effect=lambda **kw: next(responses))

        result = processor._fetch_all_content_points("test_collection")

        assert len(result) == 12000
