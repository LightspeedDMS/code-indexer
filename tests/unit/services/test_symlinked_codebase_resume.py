"""Indexer resume state:
a symlinked ``codebase_dir`` must not drop every legitimate resume
entry.

``_resume_candidate_is_safe()`` resolves the candidate (collapsing '..',
following symlinks) to check containment, then re-uses that SAME resolved
path to check eligibility via ``FileFinder.is_eligible()``. That method
computes ``file_path.relative_to(self.config.codebase_dir)`` against the
UNRESOLVED, configured ``codebase_dir`` -- so whenever ``codebase_dir`` is
itself a symlink (the Bug #1087 mount-path case: ``config.py``'s
``ConfigManager.load()`` deliberately keeps a symlinked absolute
``codebase_dir`` unresolved), the resolved candidate no longer sits under
the unresolved root and ``relative_to`` raises ``ValueError`` -- rejecting
every legitimate, in-tree resume candidate, not just out-of-tree ones.

This test drives the REAL ``SmartIndexer._do_resume_interrupted()`` against
a config loaded through the REAL ``ConfigManager`` (not a hand-built
``Config(codebase_dir=...)``) with ``codebase_dir`` pointing at a symlink,
over a real git repository, with a genuinely interrupted-after-2 resume
state. It must show BOTH halves at once: the untouched in-tree files are
still indexed, and a traversal entry is still rejected. The traversal
target uses an ELIGIBLE extension (.py) so this test discriminates on
containment, not on eligibility filtering.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.config import ConfigManager
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo_with_five_files(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    for name in ["a", "b", "c", "d", "e"]:
        (repo / f"{name}.py").write_text(f"# file {name}\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
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

    return captured.get("files", [])


class TestSymlinkedCodebaseDirResumeParity:
    """A symlinked codebase_dir must not defeat legitimate
    resume, while still rejecting traversal."""

    def test_symlinked_codebase_dir_resume_indexes_untouched_files_and_still_rejects_traversal(
        self, tmp_path: Path
    ) -> None:
        real_repo = tmp_path / "real_repo"
        _create_git_repo_with_five_files(real_repo)

        symlink_repo = tmp_path / "symlink_repo"
        symlink_repo.symlink_to(real_repo)

        outside_marker = tmp_path / "outside_marker.py"
        outside_marker_content = "# MARKER_RESUME_SYMLINKED_CODEBASE\n"
        outside_marker.write_text(outside_marker_content)

        config_path = symlink_repo / ".code-indexer" / "config.json"
        manager = ConfigManager(config_path)
        manager.create_default_config(codebase_dir=symlink_repo)

        # Load through a FRESH ConfigManager, exactly as the production
        # `cidx index` entry point does, so the Bug #1033 reconciliation
        # logic in ConfigManager.load() runs for real.
        config = ConfigManager(config_path).load()
        assert str(config.codebase_dir) == str(symlink_repo), (
            "test precondition invalid: ConfigManager did not keep "
            f"codebase_dir as the unresolved symlink path. Got: {config.codebase_dir}"
        )

        mock_embedding = MagicMock()
        metadata_path = tmp_path / "metadata.json"
        indexer = SmartIndexer(
            config=config,
            embedding_provider=mock_embedding,
            vector_store_client=_mock_vector_store(),
            metadata_path=metadata_path,
        )

        # Precondition: this extension/content combination WOULD be
        # eligible if it were in-tree -- proves the rejection below is
        # attributable to containment, not to eligibility filtering.
        decoy = symlink_repo / "_eligibility_decoy_outside_marker.py"
        decoy.write_text(outside_marker_content)
        try:
            assert indexer.file_finder.is_eligible(decoy) is True, (
                "test precondition invalid: a same-named/extensioned "
                "in-tree file must be eligible for this test to prove "
                "containment (not eligibility) drops the traversal target"
            )
        finally:
            decoy.unlink()

        # Interrupted-after-2 resume state: a, b already completed;
        # c, d, e remain -- plus one traversal entry that must stay rejected.
        md = indexer.progressive_metadata.metadata
        md["status"] = "in_progress"
        md["files_to_index"] = [
            "a.py",
            "b.py",
            "c.py",
            "d.py",
            "e.py",
            "../outside_marker.py",
        ]
        md["total_files_to_index"] = 6
        md["current_file_index"] = 2
        md["completed_files"] = ["a.py", "b.py"]
        md["failed_file_paths"] = []
        md["files_processed"] = 2
        md["chunks_indexed"] = 0

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        for name in ("c.py", "d.py", "e.py"):
            expected = (real_repo / name).resolve()
            assert expected in resolved_processed, (
                f"SECURITY/CORRECTNESS regression: a symlinked codebase_dir "
                f"must not drop legitimate in-tree resume candidate {name}. "
                f"Processed files: {processed_files}"
            )

        assert outside_marker.resolve() not in resolved_processed, (
            "SECURITY: a '../' traversal entry must still be rejected even "
            f"when codebase_dir is a symlink. Processed files: {processed_files}"
        )
