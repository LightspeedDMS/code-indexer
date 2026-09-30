"""The watch-mode incremental-processing path
(``SmartIndexer.process_files_incrementally()``, the single live method
both ``GitAwareWatchHandler`` and ``SimpleWatchHandler`` route real
filesystem-change events through) must apply the same root-containment
rule as a fresh directory walk before handing files to chunking/
embedding.

A filesystem-watch event names a file by its relative path; the join in
``process_files_incrementally`` (and the join inside
``process_branch_changes_high_throughput`` it calls into) must reject any
entry whose resolved location, following symlinks, lies outside
codebase_dir.

This test drives the REAL ``SmartIndexer.process_files_incrementally()``
over a real git repository containing a real symlink; only
``process_files_high_throughput`` is patched (to avoid real embedding-
provider network calls and chunk-store I/O). Real filesystem/git
operations throughout (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.indexing.processor import ProcessingStats
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.py").write_text("# initial\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def _make_smart_indexer(codebase_dir: Path, metadata_path: Path) -> SmartIndexer:
    # Server context: containment applies (local CLI follows symlinks).
    config = Config(codebase_dir=codebase_dir)
    config.confine_to_codebase_root()
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


def _run_process_files_incrementally(
    indexer: SmartIndexer, relative_paths: List[str]
) -> List[Path]:
    """Drive the REAL watch-mode incremental path, capturing exactly the
    file list that would be handed to chunking/embedding, without
    invoking real chunking/embedding/FTS I/O."""
    captured_files: List[Path] = []

    def _capture(files, *args, **kwargs):
        captured_files.extend(files)
        return ProcessingStats()

    with patch.object(indexer, "process_files_high_throughput", side_effect=_capture):
        indexer.process_files_incrementally(
            relative_paths,
            force_reprocess=False,
            quiet=True,
            watch_mode=True,
        )
    return captured_files


class TestWatchIncrementalRootContainment:
    def test_symlink_to_outside_file_never_reaches_processing(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        outside_target = tmp_path / "outside_via_watch.py"
        outside_target.write_text("MARKER_WATCH_OUTSIDE_CONTENT\n")
        escape_link = repo / "watch_escape_link.py"
        escape_link.symlink_to(outside_target)
        (repo / "watch_legit.py").write_text("# legit watch file\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = _run_process_files_incrementally(
            indexer, ["watch_escape_link.py", "watch_legit.py"]
        )
        resolved_captured = {Path(f).resolve(strict=False) for f in captured_files}

        assert outside_target.resolve() not in resolved_captured, (
            "A watch-event path resolving outside codebase_dir via a "
            "symlink reached process_files_high_throughput. "
            f"Captured: {captured_files}"
        )
        assert (repo / "watch_legit.py").resolve() in resolved_captured, (
            "The legitimate watch-event file must still be processed "
            f"after dropping the symlink. Captured: {captured_files}"
        )
