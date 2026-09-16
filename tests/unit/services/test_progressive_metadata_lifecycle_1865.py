"""Regression tests for ProgressiveMetadata run-lifecycle leakage (Bug #1865)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.services.high_throughput_processor import BranchIndexingResult


class _VectorStore:
    def resolve_collection_name(self, config, provider):
        return "collection"

    def ensure_provider_aware_collection(self, *args, **kwargs):
        return "collection"

    def get_collection_info(self, collection_name):
        return {"points_count": 0}

    def clear_collection(self, collection_name):
        return None

    def collection_exists(self, collection_name):
        return True


class _Provider:
    def get_provider_name(self):
        return "provider"

    def get_current_model(self):
        return "model"


class _FileFinder:
    def find_files(self):
        return []


class _Lock:
    def acquire(self, codebase_dir):
        return None

    def release(self):
        return None


class _Topology:
    def invalidate_cache(self):
        return None

    def get_current_branch(self):
        return "new-branch"

    def analyze_branch_change(self, old_branch, new_branch):
        return SimpleNamespace(files_to_reindex=[], files_to_update_metadata=[])


class _HookManager:
    def ensure_hook_installed(self):
        return None


def _fresh_metadata(path: Path) -> ProgressiveMetadata:
    return ProgressiveMetadata(path)


def test_no_files_path_does_not_persist_previous_file_lists(tmp_path, monkeypatch):
    metadata_path = tmp_path / "metadata.json"
    metadata = ProgressiveMetadata(metadata_path)
    metadata.start_indexing(
        "old-provider",
        "old-model",
        {
            "git_available": True,
            "project_id": "project",
            "current_branch": "main",
            "current_commit": "old-commit",
        },
    )
    metadata.set_files_to_index([tmp_path / "old-a.py", tmp_path / "old-b.py"])
    metadata.mark_file_completed(tmp_path / "old-a.py")

    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(codebase_dir=tmp_path)  # type: ignore[assignment]
    indexer.embedding_provider = _Provider()  # type: ignore[assignment]
    indexer.vector_store_client = _VectorStore()
    indexer.progressive_metadata = ProgressiveMetadata(metadata_path)
    indexer.file_finder = _FileFinder()  # type: ignore[assignment]
    indexer.progress_log = SimpleNamespace()
    indexer.git_topology_service = _Topology()  # type: ignore[assignment]
    monkeypatch.setattr(
        "code_indexer.services.smart_indexer.open",
        open,
        raising=False,
    )

    with pytest.raises(ValueError, match="No files found to index"):
        indexer._do_full_index(
            batch_size=50,
            progress_callback=None,
            git_status={"git_available": True, "current_branch": "main"},
            provider_name="provider",
            model_name="model",
        )

    reloaded = _fresh_metadata(metadata_path)
    assert reloaded.metadata["status"] == "completed"
    assert reloaded.metadata["files_to_index"] == []
    assert reloaded.metadata["completed_files"] == []
    assert reloaded.metadata["failed_file_paths"] == []
    assert reloaded.metadata["current_file_index"] == 0
    assert reloaded.metadata["total_files_to_index"] == 0


def test_branch_change_reports_remaining_files_for_current_run(tmp_path, monkeypatch):
    metadata_path = tmp_path / "metadata.json"
    metadata = ProgressiveMetadata(metadata_path)
    metadata.start_indexing(
        "provider",
        "model",
        {
            "git_available": True,
            "project_id": "project",
            "current_branch": "old-branch",
            "current_commit": "old-commit",
        },
    )
    metadata.set_files_to_index([tmp_path / "old-a.py", tmp_path / "old-b.py"])

    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(codebase_dir=tmp_path)  # type: ignore[assignment]
    indexer.embedding_provider = _Provider()  # type: ignore[assignment]
    indexer.vector_store_client = _VectorStore()
    indexer.progressive_metadata = ProgressiveMetadata(metadata_path)
    indexer.git_topology_service = _Topology()  # type: ignore[assignment]
    indexer.git_hook_manager = _HookManager()  # type: ignore[assignment]
    indexer.process_branch_changes_high_throughput = (  # type: ignore[method-assign]
        lambda **kwargs: BranchIndexingResult(
            files_processed=0,
            content_points_created=0,
            cancelled=False,
        )
    )
    monkeypatch.setattr(
        "code_indexer.services.smart_indexer.HNSWLIB_AVAILABLE",
        True,
    )
    monkeypatch.setattr(
        "code_indexer.services.smart_indexer.create_indexing_lock",
        lambda metadata_dir: _Lock(),
    )
    indexer.get_git_status = lambda: {  # type: ignore[method-assign]
        "git_available": True,
        "project_id": "project",
        "current_branch": "new-branch",
        "current_commit": "new-commit",
    }

    indexer.smart_index(force_full=False)

    reloaded = _fresh_metadata(metadata_path)
    assert reloaded.get_stats()["remaining_files"] == 0
    assert reloaded.metadata["files_to_index"] == []
    assert reloaded.metadata["total_files_to_index"] == 0
