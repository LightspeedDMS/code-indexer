"""Resume eligibility parity.

``_should_index_file()`` (the git-diff based filter reused by
``_resume_candidate_is_safe()``) only applies extension, exclude-dir,
exclude-pattern and override-filter checks -- it never applies
``FileFinder``'s max-file-size gate, so a resume-path candidate that is
FAR larger than ``config.indexing.max_file_size`` (and would therefore
NEVER be discovered by a fresh ``FileFinder.find_files()`` walk) was still
accepted and reprocessed on resume. Resume eligibility must match
first-run eligibility exactly, applying FileFinder's FULL decision.

Fix: ``_resume_candidate_is_safe()`` calls FileFinder's own
``is_eligible()`` (a thin public wrapper over the exact same decision
``find_files()`` itself applies) instead of reimplementing a partial
subset of it.

These tests drive the REAL ``SmartIndexer._do_resume_interrupted()`` (via
a planted ``ProgressiveMetadata`` "in_progress" state) and assert an
oversized/ineligible candidate never reaches
``process_files_high_throughput`` -- the same harness pattern as
``test_resume_path_containment.py``.
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


def _seed_resumable_metadata(indexer: SmartIndexer, files_to_index: List[str]) -> None:
    """Plant metadata exactly as a repository-authored/crash-abandoned
    .code-indexer/metadata-<provider>.json would."""
    md = indexer.progressive_metadata.metadata
    md["status"] = "in_progress"
    md["files_to_index"] = list(files_to_index)
    md["total_files_to_index"] = len(files_to_index)
    md["current_file_index"] = 0
    md["completed_files"] = []
    md["failed_file_paths"] = []
    md["files_processed"] = 0
    md["chunks_indexed"] = 0


def _run_resume_and_capture(indexer: SmartIndexer) -> List[Path]:
    """Drive the REAL resume path, capturing exactly the file list that
    would be handed to chunking/embedding, without invoking real
    chunking/embedding/FTS I/O."""
    captured: dict = {}

    def _capture(files, **kwargs):
        captured["files"] = list(files)
        from code_indexer.indexing.processor import ProcessingStats

        return ProcessingStats()

    with patch.object(indexer, "process_files_high_throughput", side_effect=_capture):
        indexer._do_resume_interrupted(
            batch_size=50,
            progress_callback=None,
            git_status={},
            provider_name="voyage-ai",
            model_name="voyage-code-3",
        )

    files: List[Path] = captured.get("files", [])
    return files


class TestResumeEligibilityMatchesFileFinderFullDecision:
    """Resume eligibility must apply FileFinder's FULL
    decision (extensions + excludes + max-file-size), not a partial
    reimplementation of it."""

    def test_oversized_file_dropped_on_resume(self, tmp_path: Path) -> None:
        """An in-tree file with an ALLOWED extension, but larger than
        config.indexing.max_file_size, would never be discovered by a
        fresh FileFinder.find_files() walk -- resume must reject it the
        same way, while still processing a legitimate small file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        indexer.config.indexing.max_file_size = 100

        oversized_file = repo / "oversized.py"
        oversized_file.write_text("x" * 1000)

        small_file = repo / "small.py"
        small_file.write_text("# small, well under the limit\n")

        _seed_resumable_metadata(indexer, ["oversized.py", "small.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert oversized_file.resolve() not in resolved_processed, (
            "An oversized file (bigger than config.indexing.max_file_size) "
            "reached process_files_high_throughput on resume. "
            f"Processed: {processed_files}"
        )
        assert small_file.resolve() in resolved_processed, (
            "The legitimate, appropriately-sized in-tree file must still "
            f"be processed. Processed files: {processed_files}"
        )

    def test_binary_disallowed_extension_file_dropped_on_resume(
        self, tmp_path: Path
    ) -> None:
        """A binary-content file whose extension is not in
        config.file_extensions must be rejected by FileFinder's full
        decision (_should_include_file), same as excluded-extension
        handling, exercised through the SAME code path the max-file-size
        fix now reuses."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())

        binary_file = repo / "payload.bin"
        binary_file.write_bytes(b"\x00\x01\x02BINARYDATA\xff\xfe")

        small_file = repo / "small.py"
        small_file.write_text("# small, eligible\n")

        _seed_resumable_metadata(indexer, ["payload.bin", "small.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert binary_file.resolve() not in resolved_processed, (
            "A binary file with a disallowed extension must be dropped "
            f"by resume. Processed files: {processed_files}"
        )
        assert small_file.resolve() in resolved_processed
