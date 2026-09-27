"""``HighThroughputProcessor.process_branch_changes_
high_throughput()`` is the single choke point every relative-path-list
caller (branch-switch detection's git-topology delta, the disk-vs-
database reconcile pass, and the watch-mode incremental path) joins onto
codebase_dir before handing files to chunking/embedding. It must reject
any candidate whose resolved location lies outside the codebase root.

This test drives the REAL ``SmartIndexer.process_branch_changes_high_
throughput()`` (inherited from ``HighThroughputProcessor``) over a real
repository containing a real symlink, using a relative-path list shaped
exactly like ``GitTopologyService.analyze_branch_change()``'s
``files_to_reindex`` output (the branch-switch caller). Only
``process_files_high_throughput`` is patched (to avoid real embedding-
provider network calls and chunk-store I/O). Real filesystem operations
throughout (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.indexing.processor import ProcessingStats
from code_indexer.services.smart_indexer import SmartIndexer


def _make_smart_indexer(codebase_dir: Path, metadata_path: Path) -> SmartIndexer:
    config = Config(codebase_dir=codebase_dir)
    mock_embedding_provider = MagicMock()
    mock_vector_store = MagicMock()
    mock_vector_store.resolve_collection_name.return_value = "test_collection"
    mock_vector_store.count_points.return_value = 0
    mock_vector_store.ensure_provider_aware_collection.return_value = "test_collection"
    mock_vector_store.begin_indexing.return_value = None
    mock_vector_store.end_indexing.return_value = {"vectors_indexed": 0}
    mock_vector_store.collection_exists.return_value = False
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=mock_vector_store,
        metadata_path=metadata_path,
    )


class TestBranchChangeRootContainment:
    def test_symlink_to_outside_file_never_reaches_processing(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        outside_target = tmp_path / "outside_via_branch_change.py"
        outside_target.write_text("MARKER_BRANCH_CHANGE_OUTSIDE_CONTENT\n")
        escape_link = repo / "branch_escape_link.py"
        escape_link.symlink_to(outside_target)
        (repo / "branch_legit.py").write_text("# legit branch file\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files: List[Path] = []

        def _capture(files, *args, **kwargs):
            captured_files.extend(files)
            return ProcessingStats()

        with patch.object(
            indexer, "process_files_high_throughput", side_effect=_capture
        ):
            indexer.process_branch_changes_high_throughput(
                old_branch="main",
                new_branch="feature",
                changed_files=["branch_escape_link.py", "branch_legit.py"],
                unchanged_files=[],
                collection_name="test_collection",
                skip_branch_isolation=True,
            )

        resolved_captured = {Path(f).resolve(strict=False) for f in captured_files}

        assert outside_target.resolve() not in resolved_captured, (
            "A branch-change relative path resolving outside codebase_dir "
            "via a symlink reached "
            f"process_files_high_throughput. Captured: {captured_files}"
        )
        assert (repo / "branch_legit.py").resolve() in resolved_captured, (
            "The legitimate branch-change file must still be processed "
            f"after dropping the symlink. Captured: {captured_files}"
        )
