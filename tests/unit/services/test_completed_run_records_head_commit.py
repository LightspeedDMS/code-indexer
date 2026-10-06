"""Every completed indexing run records the HEAD it indexed as
``current_commit``.

The refresh scheduler's stale-index self-heal compares ``current_commit``
with the working-tree HEAD and forces a reconcile when they differ. A run
that finds nothing to (re)index -- a commit touching only non-indexable
files, a no-op reconcile, an empty repository -- must still record HEAD,
or the drift signal stays set forever and every refresh cycle re-forces a
reconcile.

Real `SmartIndexer`, real `FilesystemVectorStore`, real temp git
repositories. The only test double is the embedding provider (an external
service): it counts the texts it is asked to embed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from tests.unit.services.test_reconcile_non_git_content_id_2013 import (
    _committed_git_repo,
    _git,
    _make_indexer,
    _reset,
)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_non_indexed_file(repo: Path) -> str:
    """Commit a file no indexing rule accepts; return the new HEAD."""
    (repo / "LICENSE").write_text("Example license text\n")
    _git(repo, "add", "LICENSE")
    _git(repo, "commit", "-q", "-m", "add a non-indexable file")
    return _head(repo)


class TestCompletedRunRecordsHead:
    def test_commit_touching_only_non_indexed_file_records_head(
        self, tmp_path: Path
    ) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        new_head = _commit_non_indexed_file(repo)

        _reset(embedder, store)
        indexer.smart_index(quiet=True)

        metadata = indexer.progressive_metadata.metadata
        assert embedder.embedded_texts == []
        assert metadata["status"] == "completed"
        assert metadata["current_commit"] == new_head, (
            "a run that found nothing to index must still record the HEAD it "
            "checked, or the stale-index drift signal never clears"
        )

    def test_noop_reconcile_records_head(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        new_head = _commit_non_indexed_file(repo)

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        metadata = indexer.progressive_metadata.metadata
        assert embedder.embedded_texts == []
        assert metadata["status"] == "completed"
        assert metadata["current_commit"] == new_head


class TestRunOutcomeRecorded:
    """Each finished run bumps `run_sequence` and records whether it changed
    the index (`last_run_changed_index`), so the refresh scheduler can tell
    a no-op forced reconcile apart and skip publishing a snapshot."""

    def test_noop_reconcile_records_no_change(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        metadata = indexer.progressive_metadata.metadata
        sequence_before = metadata["run_sequence"]

        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert metadata["run_sequence"] == sequence_before + 1
        assert metadata["last_run_changed_index"] is False

    def test_reconcile_reindexing_a_modified_file_records_change(
        self, tmp_path: Path
    ) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        (repo / "alpha.py").write_text("def alpha_changed():\n    return 2\n")
        _git(repo, "commit", "-q", "-am", "change alpha")

        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert indexer.progressive_metadata.metadata["last_run_changed_index"] is True

    def test_reconcile_hiding_a_deleted_file_records_change(
        self, tmp_path: Path
    ) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        _git(repo, "rm", "-q", "beta.py")
        _git(repo, "commit", "-q", "-m", "delete beta")

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == []
        assert indexer.progressive_metadata.metadata["last_run_changed_index"] is True

    def test_delete_only_incremental_run_records_change(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        _git(repo, "rm", "-q", "beta.py")
        _git(repo, "commit", "-q", "-m", "delete beta only")

        _reset(embedder, store)
        indexer.smart_index(quiet=True)

        metadata = indexer.progressive_metadata.metadata
        assert embedder.embedded_texts == []
        assert metadata["last_run_changed_index"] is True
        assert metadata["current_commit"] == _head(repo)


def _git_repo_without_indexable_files(tmp_path: Path) -> Path:
    repo = tmp_path / "emptyrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Example Tester")
    (repo / "LICENSE").write_text("Example license text\n")
    _git(repo, "add", "LICENSE")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


class TestEmptyRepositoryCompletes:
    def test_git_repo_without_indexable_files_completes(self, tmp_path: Path) -> None:
        repo = _git_repo_without_indexable_files(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert indexer.is_git_aware()

        indexer.smart_index(force_full=True, quiet=True)

        metadata = indexer.progressive_metadata.metadata
        assert metadata["status"] == "completed", (
            "an empty repository is 'completed, nothing to index', never failed"
        )
        assert metadata["current_commit"] == _head(repo)
        assert embedder.embedded_texts == []

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        assert indexer.progressive_metadata.metadata["status"] == "completed"
        assert embedder.embedded_texts == []

    def test_empty_non_git_directory_completes(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=plain,
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0, "tmp_path unexpectedly lives inside a git repo"
        indexer, embedder, store = _make_indexer(plain, tmp_path / "meta.json")
        assert not indexer.is_git_aware()

        indexer.smart_index(quiet=True)

        assert indexer.progressive_metadata.metadata["status"] == "completed"
        assert embedder.embedded_texts == []


@pytest.mark.parametrize("detected", ["unknown", "UNKNOWN", " unknown \n"])
def test_unknown_commit_keeps_recorded_commit(tmp_path: Path, detected: str) -> None:
    """A failed git-state detection ("unknown") never replaces a real
    recorded commit; the run is still recorded as finished."""
    path = tmp_path / "metadata-example-provider.json"
    recorded = "a" * 40
    metadata = ProgressiveMetadata(path)
    metadata.metadata["current_commit"] = recorded
    metadata.complete_indexing()

    metadata.record_finished_run(detected, changed_index=False)

    on_disk = ProgressiveMetadata(path).metadata
    assert on_disk["current_commit"] == recorded
    assert on_disk["run_sequence"] == 1
