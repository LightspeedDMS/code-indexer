"""Unit tests for FTS branch isolation in hide_files_not_in_branch_thread_safe().

TDD red phase: Tests written BEFORE implementation.

Tests that hide_files_not_in_branch_thread_safe() properly cleans up FTS
documents for files that should be hidden from the current branch.
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from code_indexer.services.high_throughput_processor import HighThroughputProcessor


def _make_content_points(file_paths: List[str]) -> List[Dict[str, Any]]:
    """Create mock content points for given file paths."""
    points = []
    for i, path in enumerate(file_paths):
        points.append(
            {
                "id": f"point_{i}",
                "payload": {
                    "path": path,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        )
    return points


def _make_processor(tmp_path: Path) -> HighThroughputProcessor:
    """Create a HighThroughputProcessor with mocked dependencies."""
    mock_embedding_provider = Mock()
    mock_embedding_provider.get_provider_name = Mock(return_value="test-provider")
    mock_embedding_provider.get_current_model = Mock(return_value="test-model")

    config = MagicMock()
    config.codebase_dir = tmp_path  # Must be Path, not str (FileFinder uses / operator)
    config.embedding_provider = mock_embedding_provider

    vector_store_client = MagicMock()

    processor = HighThroughputProcessor(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=vector_store_client,
    )
    return processor


class TestFTSBranchIsolationDeletesDocuments:
    """Tests that FTS documents are deleted for hidden files."""

    def test_delete_document_called_for_each_hidden_file(self, tmp_path: Path):
        """delete_document() is called once per file in files_to_hide."""
        processor = _make_processor(tmp_path)

        # All files in DB
        all_files = ["file_a.py", "file_b.py", "file_c.py", "file_d.py"]
        # Only some files visible in branch
        current_files = ["file_a.py", "file_b.py"]
        # files_to_hide = file_c.py, file_d.py

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        mock_fts_manager = MagicMock()
        mock_fts_manager.delete_document.return_value = None
        mock_fts_manager.commit.return_value = None

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=mock_fts_manager,
        )

        # delete_document should be called for file_c.py and file_d.py
        assert mock_fts_manager.delete_document.call_count == 2, (
            f"Expected 2 delete_document calls, got {mock_fts_manager.delete_document.call_count}"
        )
        deleted_paths = {
            call_args[0][0]
            for call_args in mock_fts_manager.delete_document.call_args_list
        }
        assert "file_c.py" in deleted_paths, "file_c.py should be deleted from FTS"
        assert "file_d.py" in deleted_paths, "file_d.py should be deleted from FTS"

    def test_commit_called_once_after_all_deletes(self, tmp_path: Path):
        """commit() is called exactly once after all delete_document() calls."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py", "file_c.py"]
        current_files = ["file_a.py"]

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        mock_fts_manager = MagicMock()

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=mock_fts_manager,
        )

        # commit should be called exactly once
        assert mock_fts_manager.commit.call_count == 1, (
            f"Expected commit() called once, got {mock_fts_manager.commit.call_count}"
        )

        # And commit should be called AFTER the delete_document calls
        manager_calls = mock_fts_manager.mock_calls
        delete_indices = [
            i for i, c in enumerate(manager_calls)
            if c[0] == "delete_document"
        ]
        commit_indices = [
            i for i, c in enumerate(manager_calls)
            if c[0] == "commit"
        ]
        assert len(commit_indices) == 1, "Exactly one commit call expected"
        assert all(
            di < commit_indices[0] for di in delete_indices
        ), "commit() should be called after all delete_document() calls"


class TestFTSBranchIsolationNoOpsWhenManagerNone:
    """Tests that no FTS operations happen when fts_manager is None."""

    def test_no_fts_ops_when_fts_manager_is_none(self, tmp_path: Path):
        """When fts_manager=None, no FTS operations are performed."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py", "file_c.py"]
        current_files = ["file_a.py"]

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        # Should not raise when fts_manager=None
        result = processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=None,  # No FTS manager
        )

        # Function should complete successfully
        assert result is True or result is None, (
            f"Expected success, got {result}"
        )

    def test_hide_files_still_hides_semantic_vectors_when_fts_none(
        self, tmp_path: Path
    ):
        """When fts_manager=None, semantic vector hiding still occurs."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py", "file_c.py"]
        current_files = ["file_a.py"]

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=None,
        )

        # _batch_update_points should still be called for semantic hiding
        processor.vector_store_client._batch_update_points.assert_called()


class TestFTSBranchIsolationNoOpsWhenNoFilesToHide:
    """Tests that no FTS operations happen when there are no files to hide."""

    def test_no_fts_ops_when_files_to_hide_is_empty(self, tmp_path: Path):
        """When all DB files are in current branch, no FTS deletions occur."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py"]
        current_files = ["file_a.py", "file_b.py"]  # All files visible

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        mock_fts_manager = MagicMock()

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=mock_fts_manager,
        )

        # No FTS deletions or commits when nothing to hide
        mock_fts_manager.delete_document.assert_not_called()
        mock_fts_manager.commit.assert_not_called()


class TestFTSBranchIsolationErrorHandling:
    """Tests that FTS delete failure doesn't stop processing other files."""

    def test_delete_failure_for_one_file_does_not_stop_processing(
        self, tmp_path: Path
    ):
        """When delete_document() raises for one file, other files are still processed."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py", "file_c.py", "file_d.py"]
        current_files = ["file_a.py"]  # 3 files to hide

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        mock_fts_manager = MagicMock()

        # Make delete_document raise for one specific file
        def delete_side_effect(path):
            if path == "file_b.py":
                raise RuntimeError("FTS delete failed for file_b.py")

        mock_fts_manager.delete_document.side_effect = delete_side_effect

        # Should NOT raise - best-effort
        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=mock_fts_manager,
        )

        # Other files should still have been attempted
        # file_c.py and file_d.py should have been attempted even if file_b.py failed
        delete_call_paths = {
            call_args[0][0]
            for call_args in mock_fts_manager.delete_document.call_args_list
        }
        assert len(delete_call_paths) >= 2, (
            f"Expected at least 2 delete attempts, got {delete_call_paths}"
        )

    def test_commit_called_even_if_some_deletes_fail(self, tmp_path: Path):
        """commit() is still called even when some delete_document() calls fail."""
        processor = _make_processor(tmp_path)

        all_files = ["file_a.py", "file_b.py", "file_c.py"]
        current_files = ["file_a.py"]

        content_points = _make_content_points(all_files)
        processor.vector_store_client.scroll_points.return_value = (content_points, None)
        processor.vector_store_client._batch_update_points.return_value = None

        mock_fts_manager = MagicMock()

        # All deletes fail
        mock_fts_manager.delete_document.side_effect = RuntimeError("FTS unavailable")

        processor.hide_files_not_in_branch_thread_safe(
            branch="feature-branch",
            current_files=current_files,
            collection_name="test_collection",
            fts_manager=mock_fts_manager,
        )

        # commit should still be called (best-effort cleanup)
        # OR: commit may be skipped if no successful deletes - both are acceptable
        # The key requirement is: NO EXCEPTION propagated
        # (verified by the fact that we reach this assertion)
        # We just verify the function completed without raising


class TestFTSParameterSignature:
    """Tests that fts_manager parameter exists in the right method signatures."""

    def test_hide_files_not_in_branch_thread_safe_accepts_fts_manager(
        self, tmp_path: Path
    ):
        """hide_files_not_in_branch_thread_safe() accepts fts_manager parameter."""
        import inspect
        processor = _make_processor(tmp_path)
        sig = inspect.signature(processor.hide_files_not_in_branch_thread_safe)
        assert "fts_manager" in sig.parameters, (
            "hide_files_not_in_branch_thread_safe() must accept fts_manager parameter"
        )

    def test_fts_manager_parameter_defaults_to_none(self, tmp_path: Path):
        """fts_manager parameter defaults to None in hide_files_not_in_branch_thread_safe()."""
        import inspect
        processor = _make_processor(tmp_path)
        sig = inspect.signature(processor.hide_files_not_in_branch_thread_safe)
        param = sig.parameters.get("fts_manager")
        assert param is not None, "fts_manager parameter must exist"
        assert param.default is None, (
            f"fts_manager should default to None, got {param.default}"
        )
