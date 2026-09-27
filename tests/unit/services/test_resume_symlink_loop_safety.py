"""Indexer resume state:
a symlink loop among resume-metadata candidates must not crash the
resume operation.

``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) when it
encounters a symlink loop on Python 3.9-3.12.
``SmartIndexer._resume_candidate_is_safe()`` only caught ``OSError`` around
that call, so a resume candidate that happens to be part of a circular
symlink chain (``x.py -> y.py -> x.py``) crashed the entire resume
operation instead of simply being dropped like any other unsafe candidate.

This test drives the REAL ``SmartIndexer._do_resume_interrupted()`` over a
real repository containing a genuine circular symlink pair, with resume
metadata that includes the looping entry alongside legitimate files, and
asserts the operation completes without raising while still processing the
legitimate remaining files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> None:
    """Create a minimal git repo with one initial commit."""
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


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    """Create a SmartIndexer wired to a real repo with mocked external services."""
    config = Config(codebase_dir=repo)
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


def _mock_vector_store() -> MagicMock:
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.count_points.return_value = 0
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    return store


def _seed_resumable_metadata_with_progress(
    indexer: SmartIndexer, files_to_index: List[str], current_file_index: int
) -> None:
    """Plant an "in_progress" resume state with a subset already marked
    completed via current_file_index, exactly as a genuine crash-interrupted
    run (or a repository-authored metadata file) would look."""
    metadata = indexer.progressive_metadata.metadata
    metadata["status"] = "in_progress"
    metadata["files_to_index"] = list(files_to_index)
    metadata["total_files_to_index"] = len(files_to_index)
    metadata["current_file_index"] = current_file_index
    metadata["completed_files"] = list(files_to_index[:current_file_index])
    metadata["failed_file_paths"] = []
    metadata["files_processed"] = current_file_index
    metadata["chunks_indexed"] = 0


def _run_resume_and_capture(indexer: SmartIndexer) -> List[Path]:
    """Drive the REAL resume path, capturing exactly the file list that
    would be handed to chunking/embedding, without invoking real
    chunking/embedding/FTS I/O."""
    captured_files: dict = {}

    def _capture_files(files, **kwargs):
        captured_files["files"] = list(files)
        from code_indexer.indexing.processor import ProcessingStats

        return ProcessingStats()

    with patch.object(
        indexer, "process_files_high_throughput", side_effect=_capture_files
    ):
        indexer._do_resume_interrupted(
            batch_size=50,
            progress_callback=None,
            git_status={},
            provider_name="voyage-ai",
            model_name="voyage-code-3",
        )

    files: List[Path] = captured_files.get("files", [])
    return files


class TestResumeSymlinkLoopSafety:
    """A symlink loop among resume candidates must be dropped, not crash
    the resume operation."""

    def test_symlink_loop_candidate_is_dropped_without_raising(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        (repo / "a.py").write_text("# file a\n")
        (repo / "b.py").write_text("# file b\n")
        (repo / "c.py").write_text("# file c\n")

        # A genuine circular symlink pair: x.py -> y.py -> x.py.
        x_link = repo / "x.py"
        y_link = repo / "y.py"
        x_link.symlink_to(y_link)
        y_link.symlink_to(x_link)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        # "a.py" is already completed (current_file_index=1); remaining
        # entries are b.py, x.py (the loop), c.py.
        _seed_resumable_metadata_with_progress(
            indexer,
            ["a.py", "b.py", "x.py", "c.py"],
            current_file_index=1,
        )

        # Must not raise RuntimeError (symlink loop) or any other exception.
        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve(strict=False) for f in processed_files}

        assert (repo / "b.py").resolve() in resolved_processed, (
            "The legitimate remaining file 'b.py' must still be processed "
            f"after dropping the symlink-loop candidate. Processed: {processed_files}"
        )
        assert (repo / "c.py").resolve() in resolved_processed, (
            "The legitimate remaining file 'c.py' must still be processed "
            f"after dropping the symlink-loop candidate. Processed: {processed_files}"
        )
        assert len(processed_files) == 2, (
            "Only the two legitimate remaining files should be processed; "
            f"the symlink-loop entry must be dropped. Processed: {processed_files}"
        )
