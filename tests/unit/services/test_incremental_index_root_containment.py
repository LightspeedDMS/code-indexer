"""The git-diff-based incremental discovery path
(``SmartIndexer._do_incremental_index()``) must apply the same root-
containment rule as a fresh directory walk before handing files to
chunking/embedding.

``git diff --name-status`` reports added/modified entries by name only
(git does not resolve symlink targets), and the git-diff path's own
eligibility filter (``_should_index_file``) is string-only -- it must
also classify DELETED files, so it never resolves anything. The
resulting file list therefore needs an explicit containment check before
it reaches ``process_files_high_throughput`` -- the boundary immediately
before chunking/embedding/FTS.

This test drives the REAL ``SmartIndexer._get_git_deltas_since_commit()``
and ``_do_incremental_index()`` over a real git repository containing a
real symlink; only ``process_files_high_throughput`` is patched (to avoid
real embedding-provider network calls and chunk-store I/O). Real
filesystem/git operations throughout (CLAUDE.md Foundation #1).

Note on the ``git_topology_service.is_git_available`` mock below: it
gates only the POST-processing branch-isolation call
(``hide_files_not_in_branch_thread_safe``), never git-delta detection
itself, which is driven entirely by the ``git_status["git_available"]``
dict argument passed to ``_do_incremental_index``. This exact helper
shape mirrors the existing, passing
``tests/unit/services/test_incremental_indexing_relative_paths.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.indexing.processor import ProcessingStats
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> str:
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
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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


def _run_do_incremental_index(
    indexer: SmartIndexer,
    initial_commit: str,
    second_commit: str,
    working_dir_files: Optional[List[Path]] = None,
) -> List[Path]:
    """Drive the REAL git-diff incremental path, capturing exactly the
    file list that would be handed to chunking/embedding, without
    invoking real chunking/embedding/FTS I/O."""
    captured_files: List[Path] = []

    def _capture(files, *args, **kwargs):
        captured_files.extend(files)
        return ProcessingStats()

    if working_dir_files is None:
        working_dir_files = []

    git_status = {
        "git_available": True,
        "current_branch": "master",
        "current_commit": second_commit,
        "is_dirty": False,
    }

    indexer.progressive_metadata.metadata["status"] = "completed"
    with patch.object(
        indexer.progressive_metadata,
        "can_resume_interrupted_operation",
        return_value=False,
    ):
        with patch.object(
            indexer.progressive_metadata, "get_resume_timestamp", return_value=1.0
        ):
            with patch.object(
                indexer.progressive_metadata,
                "get_last_indexed_commit",
                return_value=initial_commit,
            ):
                with patch.object(indexer.progressive_metadata, "start_indexing"):
                    with patch.object(
                        indexer.progressive_metadata, "set_files_to_index"
                    ):
                        with patch.object(
                            indexer.progressive_metadata, "update_progress"
                        ):
                            with patch.object(
                                indexer.progressive_metadata,
                                "update_commit_watermark",
                            ):
                                with patch.object(
                                    indexer.progressive_metadata,
                                    "complete_indexing",
                                ):
                                    with patch.object(
                                        indexer,
                                        "_delete_files_from_backend",
                                        return_value=0,
                                    ):
                                        with patch.object(
                                            indexer.file_finder,
                                            "find_modified_files",
                                            return_value=working_dir_files,
                                        ):
                                            with patch.object(
                                                indexer.file_finder,
                                                "find_files",
                                                return_value=[],
                                            ):
                                                with patch.object(
                                                    indexer.git_topology_service,
                                                    "get_current_branch",
                                                    return_value="master",
                                                ):
                                                    with patch.object(
                                                        indexer.git_topology_service,
                                                        "is_git_available",
                                                        return_value=False,
                                                    ):
                                                        with patch.object(
                                                            indexer.progress_log,
                                                            "start_session",
                                                            return_value="s",
                                                        ):
                                                            with patch.object(
                                                                indexer.progress_log,
                                                                "complete_session",
                                                            ):
                                                                with patch.object(
                                                                    indexer,
                                                                    "process_files_high_throughput",
                                                                    side_effect=_capture,
                                                                ):
                                                                    indexer._do_incremental_index(
                                                                        batch_size=10,
                                                                        progress_callback=None,
                                                                        git_status=git_status,
                                                                        provider_name="voyage",
                                                                        model_name="voyage-3",
                                                                        safety_buffer_seconds=0,
                                                                        quiet=True,
                                                                    )
    return captured_files


class TestIncrementalGitDiffRootContainment:
    def test_committed_symlink_to_outside_file_never_reaches_processing(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        initial_commit = _create_git_repo(repo)

        outside_target = tmp_path / "outside_via_commit.py"
        outside_target.write_text("MARKER_INCREMENTAL_OUTSIDE_CONTENT\n")
        escape_link = repo / "escape_link.py"
        escape_link.symlink_to(outside_target)
        (repo / "legit_incremental.py").write_text("# legit\n")

        second_commit = _commit_all(repo, "add symlink and legit file")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = _run_do_incremental_index(
            indexer, initial_commit, second_commit
        )
        resolved_captured = {Path(f).resolve(strict=False) for f in captured_files}

        assert outside_target.resolve() not in resolved_captured, (
            "A symlink committed via git, resolving outside codebase_dir, "
            "reached process_files_high_throughput via the "
            f"incremental git-diff path. Captured: {captured_files}"
        )
        assert (repo / "legit_incremental.py").resolve() in resolved_captured, (
            "The legitimate committed file must still be processed after "
            f"dropping the symlink. Captured: {captured_files}"
        )
