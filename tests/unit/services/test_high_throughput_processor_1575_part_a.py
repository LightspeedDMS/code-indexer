"""TDD tests for Bug #1575 Part A -- high_throughput_processor.py wiring.

Covers:
  - AC13: `_fetch_all_content_points()` is no longer called on the refresh
    path (`hide_files_not_in_branch_thread_safe()` with no pre-fetched
    `all_content_points`) -- `distinct_content_paths()` /
    `fetch_points_for_paths()` are used instead.
  - AC6: absolute stored paths resolve identically in discovery AND in
    update matching. The discriminating input is an ABSOLUTE stored path
    (relative-only inputs pass on both correct and broken code, per the
    issue's own methodology note).

RED phase: every test in this file must FAIL against pre-Part-A
`hide_files_not_in_branch_thread_safe()` (which still calls
`_fetch_all_content_points()`/`scroll_points()`, never
`distinct_content_paths()`/`fetch_points_for_paths()`).
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock

from code_indexer.services.high_throughput_processor import HighThroughputProcessor


def _make_processor(tmp_path: Path) -> HighThroughputProcessor:
    mock_embedding_provider = Mock()
    mock_embedding_provider.get_provider_name = Mock(return_value="test-provider")
    mock_embedding_provider.get_current_model = Mock(return_value="test-model")

    config = MagicMock()
    config.codebase_dir = tmp_path
    config.embedding_provider = mock_embedding_provider

    vector_store_client = MagicMock()

    return HighThroughputProcessor(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=vector_store_client,
    )


class TestRefreshPathUsesTargetedEnumeration:
    """AC13: the refresh path (all_content_points=None) must use
    distinct_content_paths()/fetch_points_for_paths(), never
    _fetch_all_content_points()/scroll_points().
    """

    def test_distinct_content_paths_called_not_scroll_points(self, tmp_path):
        processor = _make_processor(tmp_path)
        processor.vector_store_client.distinct_content_paths.return_value = {
            "src/old.py"
        }
        processor.vector_store_client.fetch_points_for_paths.return_value = [
            {
                "id": "point_1",
                "payload": {
                    "path": "src/old.py",
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ]
        processor.vector_store_client._batch_update_payload_only.return_value = True

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=["src/new.py"],
            collection_name="test_collection",
        )

        processor.vector_store_client.distinct_content_paths.assert_called_once_with(
            "test_collection"
        )
        processor.vector_store_client.scroll_points.assert_not_called()

    def test_fetch_points_for_paths_called_with_paths_to_hide(self, tmp_path):
        processor = _make_processor(tmp_path)
        processor.vector_store_client.distinct_content_paths.return_value = {
            "src/old.py"
        }
        processor.vector_store_client.fetch_points_for_paths.return_value = [
            {
                "id": "point_1",
                "payload": {
                    "path": "src/old.py",
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ]
        processor.vector_store_client._batch_update_payload_only.return_value = True

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=["src/new.py"],
            collection_name="test_collection",
        )

        processor.vector_store_client.fetch_points_for_paths.assert_called_once()
        call_args = processor.vector_store_client.fetch_points_for_paths.call_args
        assert call_args[0][0] == "test_collection"
        assert set(call_args[0][1]) == {"src/old.py"}


class TestAbsoluteStoredPathHiddenCorrectly:
    """AC6: an ABSOLUTE stored path (the discriminating input, per the
    issue text) must still be correctly identified for hiding AND matched
    during the update step -- a relative-only test would pass on both
    correct and broken code.
    """

    def test_absolute_stored_path_gets_hidden_branches_updated(self, tmp_path):
        processor = _make_processor(tmp_path)
        abs_path = str(tmp_path / "src" / "old_file.py")

        processor.vector_store_client.distinct_content_paths.return_value = {abs_path}
        processor.vector_store_client.fetch_points_for_paths.return_value = [
            {
                "id": "point_1",
                "payload": {
                    "path": abs_path,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ]
        processor.vector_store_client._batch_update_payload_only.return_value = True

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=["src/new_file.py"],
            collection_name="test_collection",
        )

        processor.vector_store_client._batch_update_payload_only.assert_called_once()
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates = call_args[0][0]
        assert len(updates) == 1
        assert updates[0]["id"] == "point_1"
        assert "feature-branch" in updates[0]["payload"]["hidden_branches"]

    def test_absolute_stored_path_matching_current_branch_stays_visible(self, tmp_path):
        """An absolute stored path that DOES resolve (relative to the
        project root) to a file present in the current branch must NOT be
        hidden -- proving discovery and matching agree in BOTH directions.
        """
        processor = _make_processor(tmp_path)
        abs_path = str(tmp_path / "src" / "still_here.py")

        processor.vector_store_client.distinct_content_paths.return_value = {abs_path}
        processor.vector_store_client._batch_update_payload_only.return_value = True

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=["src/still_here.py"],
            collection_name="test_collection",
        )

        processor.vector_store_client.fetch_points_for_paths.assert_not_called()
        processor.vector_store_client._batch_update_payload_only.assert_not_called()


class TestBatchEnsureFilesVisibleAbsolutePathMatch:
    """AC6 (symmetric fix): _batch_ensure_files_visible_in_branch() must
    also normalize an absolute stored payload.path before comparing it
    against the (relative) unchanged_set.
    """

    def test_absolute_stored_path_hidden_branches_updated(self, tmp_path):
        processor = _make_processor(tmp_path)
        abs_path = str(tmp_path / "src" / "unchanged_file.py")

        all_content_points = [
            {
                "id": "point_1",
                "payload": {
                    "path": abs_path,
                    "type": "content",
                    "hidden_branches": ["feature-x"],
                },
            }
        ]

        processor._batch_ensure_files_visible_in_branch(
            file_paths=["src/unchanged_file.py"],
            branch="feature-x",
            collection_name="test_collection",
            all_content_points=all_content_points,
        )

        processor.vector_store_client._batch_update_payload_only.assert_called_once()
        call_args = processor.vector_store_client._batch_update_payload_only.call_args
        updates = call_args[0][0]
        assert len(updates) == 1
        assert updates[0]["id"] == "point_1"
        assert "feature-x" not in updates[0]["payload"]["hidden_branches"]


class TestNormalizeStoredPathDelegatesToSharedHelper:
    """Codex review follow-up (Bug #1575 Part A, CRITICAL finding 2
    remediation): ``_normalize_stored_path`` must delegate to the SAME
    shared normalization function the ``hnsw_index_manager.py`` fix uses
    (``_normalize_stored_path_for_visibility``), rather than maintaining a
    second, independently-drifting reimplementation of the exact same
    absolute-vs-relative stored-path logic.
    """

    def test_delegates_to_shared_normalize_helper(self, tmp_path, monkeypatch):
        from code_indexer.storage import hnsw_index_manager

        processor = _make_processor(tmp_path)

        called_with = {}
        original = hnsw_index_manager._normalize_stored_path_for_visibility

        def spy(path, project_root):
            called_with["path"] = path
            called_with["project_root"] = project_root
            return original(path, project_root)

        monkeypatch.setattr(
            hnsw_index_manager, "_normalize_stored_path_for_visibility", spy
        )

        absolute_path = str(tmp_path / "src" / "a.py")
        result = processor._normalize_stored_path(absolute_path)

        assert called_with.get("path") == absolute_path
        assert called_with.get("project_root") == tmp_path
        assert result == "src/a.py"

    def test_relative_path_passthrough_unchanged(self, tmp_path):
        processor = _make_processor(tmp_path)
        assert processor._normalize_stored_path("src/a.py") == "src/a.py"

    def test_path_outside_project_root_returned_unchanged(self, tmp_path):
        processor = _make_processor(tmp_path)
        outside_path = "/some/other/root/src/a.py"
        assert processor._normalize_stored_path(outside_path) == outside_path
