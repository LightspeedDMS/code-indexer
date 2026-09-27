"""Indexer resume state:
a resume candidate's symlink NAME must be eligible too, not just its
resolved TARGET.

``_resume_candidate_is_safe()`` checked eligibility only on the RESOLVED
target of a candidate. A symlink whose target is a perfectly eligible,
in-tree file can still have a NAME that a fresh ``FileFinder.find_files()``
walk would never reach at all -- because ``find_files()`` prunes excluded
directories (e.g. ``node_modules/``) and matches ``.gitignore`` patterns by
NAME, before it ever looks at (or resolves) a symlink's target. Resume must
apply the same name-based exclusion, or it can resurface content a normal
walk would never have indexed in the first place.

This test drives the REAL ``SmartIndexer._do_resume_interrupted()`` over a
real repository containing such symlinks and asserts: (1) a link inside an
excluded directory is dropped even though its target is eligible, (2) a
link whose name matches a ``.gitignore`` pattern is dropped even though its
target is eligible, and (3) a legitimate in-tree link -- eligible name AND
eligible target -- is still processed (no over-correction).
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
    metadata = indexer.progressive_metadata.metadata
    metadata["status"] = "in_progress"
    metadata["files_to_index"] = list(files_to_index)
    metadata["total_files_to_index"] = len(files_to_index)
    metadata["current_file_index"] = 0
    metadata["completed_files"] = []
    metadata["failed_file_paths"] = []
    metadata["files_processed"] = 0
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


class TestResumeSymlinkLinkNameEligibility:
    """Resume must reject a candidate whose symlink NAME
    is excluded/gitignored, even when its resolved target is eligible."""

    def test_symlink_inside_excluded_directory_dropped_despite_eligible_target(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        src_dir = repo / "src"
        src_dir.mkdir()
        target_file = src_dir / "a.py"
        target_file.write_text("# eligible in-tree target\n")

        node_modules_dir = repo / "node_modules"
        node_modules_dir.mkdir()
        excluded_name_link = node_modules_dir / "x.py"
        excluded_name_link.symlink_to(Path("..") / "src" / "a.py")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["node_modules/x.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert target_file.resolve() not in resolved_processed, (
            "A symlink whose NAME sits inside an excluded directory "
            "(node_modules/) must be dropped by resume the same way a "
            "fresh FileFinder walk would never reach it, even though its "
            f"resolved target is eligible. Processed: {processed_files}"
        )
        assert not processed_files, (
            f"Expected nothing processed for this resume run. Processed: {processed_files}"
        )

    def test_gitignored_symlink_name_dropped_despite_eligible_target(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        src_dir = repo / "src"
        src_dir.mkdir()
        target_file = src_dir / "a.py"
        target_file.write_text("# eligible in-tree target\n")

        (repo / ".gitignore").write_text("ignored_name.py\n")

        gitignored_link = repo / "ignored_name.py"
        gitignored_link.symlink_to(Path("src") / "a.py")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["ignored_name.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert target_file.resolve() not in resolved_processed, (
            "A symlink whose NAME matches a .gitignore pattern must be "
            "dropped by resume, even though its resolved target is "
            f"eligible. Processed: {processed_files}"
        )
        assert not processed_files, (
            f"Expected nothing processed for this resume run. Processed: {processed_files}"
        )

    def test_legitimate_in_tree_symlink_eligible_name_and_target_kept(
        self, tmp_path: Path
    ) -> None:
        """No over-correction: a real in-tree symlink whose NAME and
        TARGET are both eligible must still be processed on resume."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        src_dir = repo / "src"
        src_dir.mkdir()
        target_file = src_dir / "a.py"
        target_file.write_text("# eligible in-tree target\n")

        legit_link = repo / "legit_link.py"
        legit_link.symlink_to(Path("src") / "a.py")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["legit_link.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert target_file.resolve() in resolved_processed, (
            "A legitimate in-tree symlink with an eligible name AND an "
            "eligible target must still be processed on resume. "
            f"Processed: {processed_files}"
        )
