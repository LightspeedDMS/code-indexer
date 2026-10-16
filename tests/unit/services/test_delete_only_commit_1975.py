"""Issue #1975: a delete-only commit must not lead to a spurious
"inconsistent state" and a full re-index.

Real `SmartIndexer`, real `FilesystemVectorStore`, real temp git repository.
The only test double is the embedding provider (an external service): it
counts the texts it is asked to embed. Harness shared with the Issue #2013
reconcile tests.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from tests.unit.services.index_visibility_test_support import (
    InfoRecorder as _InfoRecorder,
    hidden_branches_by_path as _hidden_branches_by_path,
    reconciled as _reconciled,
)
from tests.unit.services.test_reconcile_non_git_content_id_2013 import (
    _BASE_MTIME,
    _committed_git_repo,
    _git,
    _make_indexer,
    _reset,
)


class TestDeleteOnlyCommit:
    def test_delete_only_commit_three_incremental_runs_never_reembed(
        self, tmp_path: Path
    ) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert indexer.is_git_aware()
        indexer.smart_index(force_full=True, quiet=True)
        assert embedder.embedded_texts, "initial full index must embed the files"

        _git(repo, "rm", "-q", "beta.py")
        _git(repo, "commit", "-q", "-m", "delete beta only")

        for run in range(1, 4):
            _reset(embedder, store)
            recorder = _InfoRecorder()
            indexer.smart_index(quiet=True, progress_callback=recorder)
            assert embedder.embedded_texts == [], (
                f"incremental run {run} after a delete-only commit re-embedded "
                f"content: {embedder.embedded_texts}"
            )
            inconsistent = [m for m in recorder.messages if "nconsistent" in m]
            assert inconsistent == [], (
                f"incremental run {run} reported an inconsistent state: {inconsistent}"
            )
            assert indexer.progressive_metadata.metadata["status"] == "completed"

        hidden = _hidden_branches_by_path(indexer, store)
        assert "master" in hidden.get("beta.py", set()), (
            f"the deleted file's points must be hidden on master: {hidden}"
        )
        for kept in ("alpha.py", "gamma.py"):
            assert "master" not in hidden[kept], (
                f"{kept} still exists and must stay visible: {hidden}"
            )
        head = indexer.get_git_status()["current_commit"]
        assert indexer.progressive_metadata.get_last_indexed_commit("master") == head

    def test_delete_only_run_keeps_counters_and_processes_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        metadata = indexer.progressive_metadata.metadata
        files_before = metadata["files_processed"]
        chunks_before = metadata["chunks_indexed"]
        assert files_before > 0 and chunks_before > 0

        _git(repo, "rm", "-q", "beta.py")
        _git(repo, "commit", "-q", "-m", "delete beta only")

        _reset(embedder, store)
        with caplog.at_level(logging.WARNING):
            indexer.smart_index(quiet=True)

        assert embedder.embedded_texts == []
        assert "No files to process" not in caplog.text, (
            "a delete-only run must not start an empty file-processing pass"
        )
        metadata = indexer.progressive_metadata.metadata
        assert metadata["status"] == "completed"
        assert metadata["files_processed"] == files_before, (
            "a delete-only run must not reset the processed-file counter"
        )
        assert metadata["chunks_indexed"] == chunks_before
        head = indexer.get_git_status()["current_commit"]
        assert indexer.progressive_metadata.get_last_indexed_commit("master") == head

    def test_previously_affected_completed_zero_count_metadata_is_not_wiped(
        self, tmp_path: Path
    ) -> None:
        """Metadata already written by a pre-fix delete-only run (completed,
        files_processed 0) must not force a full re-index after upgrade."""
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        progressive = indexer.progressive_metadata
        progressive.metadata["files_processed"] = 0
        progressive.metadata["chunks_indexed"] = 0
        progressive._save_metadata()

        reconciled_per_run = []
        for _ in range(2):
            _reset(embedder, store)
            recorder = _InfoRecorder()
            indexer.smart_index(quiet=True, progress_callback=recorder)
            assert not any("nconsistent" in m for m in recorder.messages)
            assert embedder.embedded_texts == []
            assert indexer.progressive_metadata.metadata["status"] == "completed"
            reconciled_per_run.append(_reconciled(recorder))
        assert reconciled_per_run == [True, False], (
            "the ambiguous state is verified by ONE reconcile, then trusted"
        )

    def test_explicit_reconcile_marks_store_verified(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        progressive = indexer.progressive_metadata
        progressive.metadata["files_processed"] = 0
        progressive._save_metadata()

        indexer.smart_index(
            reconcile_with_database=True, quiet=True, files_count_to_process=1
        )
        assert not progressive.metadata["store_verified_by_reconcile"], (
            "a file-count-limited reconcile did not verify the whole store"
        )

        indexer.smart_index(reconcile_with_database=True, quiet=True)
        assert progressive.metadata["store_verified_by_reconcile"]

        _reset(embedder, store)
        recorder = _InfoRecorder()
        indexer.smart_index(quiet=True, progress_callback=recorder)
        assert embedder.embedded_texts == []
        assert not _reconciled(recorder), (
            "a routine run after a completed explicit reconcile must not reconcile"
        )

    def test_cancelled_reconcile_does_not_verify_store(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        progressive = indexer.progressive_metadata
        progressive.metadata["files_processed"] = 0
        progressive._save_metadata()
        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )
        assert store.delete_by_filter(
            collection, {"must": [{"key": "path", "match": {"value": "gamma.py"}}]}
        )

        def interrupt_on_file_progress(
            current: int, total: int, path: Path, **kwargs: Any
        ) -> Any:
            return "INTERRUPT" if total > 0 else None

        stats = indexer.smart_index(
            quiet=True, progress_callback=interrupt_on_file_progress
        )

        assert stats.cancelled
        assert not progressive.metadata["store_verified_by_reconcile"], (
            "a cancelled reconcile did not verify the store"
        )

    def test_fresh_run_resets_verified_flag(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        progressive = indexer.progressive_metadata
        assert progressive.metadata["store_verified_by_reconcile"]

        alpha = repo / "alpha.py"
        alpha.write_text("def alpha():\n    return 'ALPHA_V2'\n")
        # Old mtime: the final run below must see no working-dir change.
        os.utime(alpha, (_BASE_MTIME + 1000, _BASE_MTIME + 1000))
        _git(repo, "commit", "-q", "-am", "change alpha")
        indexer.smart_index(quiet=True)
        assert not progressive.metadata["store_verified_by_reconcile"], (
            "a run that started from zero must clear the verification"
        )

        # That run's counters are lost (e.g. it was interrupted and the
        # zero-count state is all that is left): the check must run again.
        progressive.metadata["files_processed"] = 0
        progressive._save_metadata()
        recorder = _InfoRecorder()
        indexer.smart_index(quiet=True, progress_callback=recorder)
        assert _reconciled(recorder)

    @pytest.mark.parametrize("left_status", ["completed", "in_progress"])
    def test_ambiguous_zero_count_state_reconciles_partial_store(
        self, tmp_path: Path, left_status: str
    ) -> None:
        """Stored chunks with files_processed 0 cannot prove the store is
        complete (an interrupted run may have left it partial), whatever the
        status says. The run reconciles: the missing file is indexed, present
        files are not re-embedded, nothing is wiped."""
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)

        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )
        assert store.delete_by_filter(
            collection, {"must": [{"key": "path", "match": {"value": "gamma.py"}}]}
        )
        progressive = indexer.progressive_metadata
        progressive.metadata["status"] = left_status
        progressive.metadata["files_processed"] = 0
        progressive.metadata["files_to_index"] = []
        progressive.metadata["current_file_index"] = 0
        progressive._save_metadata()

        _reset(embedder, store)
        recorder = _InfoRecorder()
        indexer.smart_index(quiet=True, progress_callback=recorder)

        assert not any("nconsistent" in m for m in recorder.messages)
        assert embedder.embedded_texts, "the missing file must be indexed"
        assert all("GAMMA_MARKER" in t for t in embedder.embedded_texts), (
            f"only the missing file may be embedded: {embedder.embedded_texts}"
        )
        assert set(_hidden_branches_by_path(indexer, store)) == {
            "alpha.py",
            "beta.py",
            "gamma.py",
        }
        assert indexer.progressive_metadata.metadata["status"] == "completed"

        _reset(embedder, store)
        recorder = _InfoRecorder()
        indexer.smart_index(quiet=True, progress_callback=recorder)
        assert embedder.embedded_texts == []
        assert not _reconciled(recorder), "a verified store must not reconcile again"
