"""Indexer resume-state trust and containment.

Server-spawned indexing (golden-repo add/refresh,
activated-repo reindex) must not trust repository-authored resume state at
all. The chosen mechanism (see smart_indexer.py) is a new
``trust_resume_state`` parameter on ``SmartIndexer.smart_index()``: when
False, the resume branch (``ProgressiveMetadata.can_resume_interrupted_operation()``
-> ``_do_resume_interrupted()``) is never taken, regardless of what a
committer/tenant-authored ``.code-indexer/metadata-<provider>.json`` claims
-- indexing falls through to the normal (self-computed) incremental/full
walk instead. This does NOT use ``--clear``/``force_full`` (which would
force a full re-embed on every server-spawned run -- prohibitively
expensive at ~900-repo production scale) and does not discard the metadata
file itself; a normal incremental/full run overwrites
``files_to_index``/``status`` with its own self-computed, safe list anyway
(see ``ProgressiveMetadata.set_files_to_index`` call sites), so the
repository-authored state is naturally superseded.

These tests drive the REAL ``SmartIndexer.smart_index()`` entry point
against planted repository-authored resume state and assert the resume
branch is skipped when ``trust_resume_state=False``, and still honored
(backward compatible) when omitted/True.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
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


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    config = Config(codebase_dir=repo)
    mock_embedding = MagicMock()
    # smart_index() (unlike _do_resume_interrupted() called directly) reads
    # these into ProgressiveMetadata and JSON-serializes them -- must be
    # real strings, not an unconfigured MagicMock.
    mock_embedding.get_provider_name.return_value = "voyage-ai"
    mock_embedding.get_current_model.return_value = "voyage-code-3"
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


def _seed_resumable_metadata(indexer: SmartIndexer, files_to_index) -> None:
    md = indexer.progressive_metadata.metadata
    md["status"] = "in_progress"
    md["files_to_index"] = list(files_to_index)
    md["total_files_to_index"] = len(files_to_index)
    md["current_file_index"] = 0
    md["completed_files"] = []
    md["failed_file_paths"] = []
    md["files_processed"] = 0
    md["chunks_indexed"] = 0


class TestTrustResumeStateFlag:
    """Server-spawned runs must be able to distrust
    repository-authored resume state without forcing a full re-embed."""

    def test_trust_resume_state_false_skips_resume_branch(self, tmp_path: Path) -> None:
        """With trust_resume_state=False, repository-planted 'in_progress'
        resume state must NOT route into _do_resume_interrupted, even
        though can_resume_interrupted_operation() would return True. Since
        the metadata shows an interrupted operation, the fix routes to
        _do_reconcile_with_database (a fresh disk-vs-database walk) rather
        than the mtime-scan-based _do_incremental_index -- see
        test_resume_interrupted_reconcile_fallback.py for the real,
        unmocked end-to-end reproduction of why that distinction matters."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        assert indexer.progressive_metadata.can_resume_interrupted_operation() is False
        _seed_resumable_metadata(indexer, ["../outside_secret.txt"])
        assert indexer.progressive_metadata.can_resume_interrupted_operation() is True

        with (
            patch.object(indexer, "_do_resume_interrupted") as mock_resume,
            patch.object(indexer, "_do_incremental_index") as mock_incremental,
            patch.object(indexer, "_do_reconcile_with_database") as mock_reconcile,
        ):
            indexer.smart_index(force_full=False, trust_resume_state=False)

        mock_resume.assert_not_called()
        mock_incremental.assert_not_called()
        mock_reconcile.assert_called_once()

    def test_trust_resume_state_default_true_uses_resume_branch(
        self, tmp_path: Path
    ) -> None:
        """Backward compatibility: omitting trust_resume_state (default
        True) must preserve the existing resume behavior for plain
        `cidx index` runs."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["initial.py"])

        with (
            patch.object(indexer, "_do_resume_interrupted") as mock_resume,
            patch.object(indexer, "_do_incremental_index") as mock_incremental,
        ):
            indexer.smart_index(force_full=False)

        mock_resume.assert_called_once()
        mock_incremental.assert_not_called()

    def test_trust_resume_state_false_compatible_with_reconcile(
        self, tmp_path: Path
    ) -> None:
        """`--reconcile
        --ignore-resume-state` must be a valid, compatible combination.
        smart_index()'s resume-branch check (gated by trust_resume_state)
        runs BEFORE the reconcile check, so trust_resume_state=False must
        skip the resume branch regardless of reconcile_with_database, and
        reconcile_with_database=True must still route to
        _do_reconcile_with_database (reconcile's own DB-comparison logic is
        an entirely separate, already-safe mechanism that never reads
        files_to_index)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["../outside_secret.txt"])
        assert indexer.progressive_metadata.can_resume_interrupted_operation() is True

        with (
            patch.object(indexer, "_do_resume_interrupted") as mock_resume,
            patch.object(indexer, "_do_reconcile_with_database") as mock_reconcile,
        ):
            indexer.smart_index(
                force_full=False,
                reconcile_with_database=True,
                trust_resume_state=False,
            )

        mock_resume.assert_not_called()
        mock_reconcile.assert_called_once()
