"""The disk-vs-database reconcile pass
(``SmartIndexer._do_reconcile_with_database()``) must never treat a
symlink resolving outside the codebase root as a "missing" file to index.

``_do_reconcile_with_database()`` builds its candidate list from
``FileFinder.find_files()`` (``all_files_to_index``), so this test is
primarily a direct-path confirmation that ``FileFinder``'s own
containment check is actually exercised by the reconcile call chain,
down to the boundary immediately before chunking/embedding
(``process_files_high_throughput``, reached here via
``process_branch_changes_high_throughput``).

Real filesystem operations throughout (CLAUDE.md Foundation #1); only the
database-snapshot and embedding/chunking boundaries are mocked, since a
real vector store / embedding provider is out of scope for a unit test.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.services.high_throughput_processor import BranchIndexingResult
from code_indexer.services.smart_indexer import SmartIndexer


def _make_smart_indexer(codebase_dir: Path, metadata_path: Path) -> SmartIndexer:
    # Server context: containment applies (local CLI follows symlinks).
    config = Config(codebase_dir=codebase_dir)
    config.confine_to_codebase_root()
    mock_embedding_provider = MagicMock()
    mock_vector_store = MagicMock()
    mock_vector_store.resolve_collection_name.return_value = "test_collection"
    mock_vector_store.ensure_provider_aware_collection.return_value = "test_collection"
    mock_vector_store.count_points.return_value = 0
    mock_vector_store.begin_indexing.return_value = None
    mock_vector_store.end_indexing.return_value = {"vectors_indexed": 0}
    mock_vector_store.collection_exists.return_value = False
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=mock_vector_store,
        metadata_path=metadata_path,
    )


class TestReconcileRootContainment:
    def test_symlink_to_outside_file_never_queued_or_processed(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        outside_target = tmp_path / "outside_via_reconcile.py"
        outside_target.write_text("MARKER_RECONCILE_OUTSIDE_CONTENT\n")
        escape_link = repo / "reconcile_escape_link.py"
        escape_link.symlink_to(outside_target)
        (repo / "reconcile_legit.py").write_text("# legit reconcile file\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_changed_files: List[str] = []

        def _capture(*args, **kwargs):
            captured_changed_files.extend(kwargs.get("changed_files", []))
            return BranchIndexingResult()

        with patch.object(indexer, "_get_indexed_files_snapshot", return_value={}):
            with patch.object(indexer.progressive_metadata, "complete_indexing"):
                with patch.object(
                    indexer,
                    "process_branch_changes_high_throughput",
                    side_effect=_capture,
                ):
                    indexer._do_reconcile_with_database(
                        batch_size=10,
                        progress_callback=None,
                        git_status={},
                        provider_name="voyage",
                        model_name="voyage-3",
                        quiet=True,
                    )

        resolved_captured = {
            (repo / f).resolve(strict=False) for f in captured_changed_files
        }

        assert outside_target.resolve() not in resolved_captured, (
            "A symlink resolving outside codebase_dir was queued as a "
            "reconcile changed_files entry, headed toward "
            f"process_files_high_throughput. Captured: {captured_changed_files}"
        )
        assert (repo / "reconcile_legit.py").resolve() in resolved_captured, (
            "The legitimate on-disk file must still be reconciled after "
            f"dropping the symlink. Captured: {captured_changed_files}"
        )
