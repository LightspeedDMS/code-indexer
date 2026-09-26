"""Indexer resume-state trust and containment.

Correctness regression guard (Bug #1218-class
silent partial index): ``mark_file_completed()``/``update_progress()``
advance ``last_index_timestamp`` per file as an interrupted run processes
each one. When ``trust_resume_state=False`` skips the resume branch after
such an interruption, ``_do_incremental_index``'s mtime scan compares file
mtimes against a ``last_index_timestamp`` the interrupted run already
advanced PAST the untouched files' mtimes -- so those files are silently
never rediscovered. Status stays "in_progress" forever with 0 files
processed on every subsequent run.

Fix: when ``trust_resume_state=False`` AND the metadata shows an
interrupted operation, discard the resume state and route to
``_do_reconcile_with_database`` -- a fresh disk-vs-database walk that never
reads ``files_to_index``/``last_index_timestamp`` -- instead of the
incremental mtime-scan path.

This is a REAL end-to-end reproduction: a real git repository, a real
``FilesystemVectorStore``, and a real deterministic (non-network) embedding
provider, driven through the actual ``SmartIndexer.smart_index()``
production entry point.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

_VECTOR_DIM = 16
_BYTE_MAX_VALUE = 255.0
_VECTOR_SCALE = 2.0
_VECTOR_OFFSET = 1.0
_FAKE_MAX_TOKENS = 8192
_PAST_MTIME_OFFSET_SECONDS = (
    3600  # 1 hour in the past -- deterministic, not timing-dependent
)
_SAFETY_BUFFER_SECONDS = 60  # smart_index()'s default safety_buffer_seconds


def _deterministic_embedding(text: str) -> List[float]:
    """Real (non-mocked), deterministic local embedding: no network call."""
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / _BYTE_MAX_VALUE) * _VECTOR_SCALE - _VECTOR_OFFSET
        for i in range(_VECTOR_DIM)
    ]


class _DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working EmbeddingProvider (no mocking) -- local test-only
    duplicate of the identical helper in
    test_smart_indexer_1575_part_c_defect1_wiring.py (kept local per this
    project's own precedent rather than a cross-test-module import)."""

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return _deterministic_embedding(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [_deterministic_embedding(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=_deterministic_embedding(text), model=self.get_current_model()
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[_deterministic_embedding(t) for t in texts],
            model=self.get_current_model(),
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, int]:
        return {"dimensions": _VECTOR_DIM, "max_tokens": _FAKE_MAX_TOKENS}

    def get_provider_name(self) -> str:
        return "deterministic-test-provider"

    def get_current_model(self) -> str:
        return "deterministic-test-model"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return _FAKE_MAX_TOKENS


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _make_indexer(repo: Path, metadata_path: Path) -> SmartIndexer:
    config = Config(codebase_dir=str(repo))
    embedding_provider = _DeterministicHashEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=metadata_path,
    )


def _build_repo_with_five_files(repo: Path) -> List[Path]:
    """Real git repo with 5 committed files, each with unique content, all
    mtimes set to a deterministic PAST timestamp (never tied to real
    wall-clock timing, so the reproduction is not flaky)."""
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-indexer/\n")

    files = []
    for i, name in enumerate(["a", "b", "c", "d", "e"]):
        f = repo / f"{name}.py"
        f.write_text(f"# file {name} unique content marker {i}\n")
        files.append(f)

    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial: five files")

    past_time = time.time() - _PAST_MTIME_OFFSET_SECONDS
    for f in files:
        os.utime(f, (past_time, past_time))

    return files


def _simulate_interrupted_run_after_two_files(
    indexer: SmartIndexer, files: List[Path]
) -> None:
    """Real ProgressiveMetadata calls (not mocked) reproducing the EXACT
    state a genuine interrupted run leaves behind: files_to_index carries
    all 5, the first 2 are marked completed (advancing
    last_index_timestamp to "now" via the real mark_file_completed()),
    status stays "in_progress", current_file_index=2. Mirrors the
    established direct-metadata-manipulation pattern in
    tests/unit/infrastructure/test_resumability_simple.py."""
    git_status = indexer.get_git_status()
    indexer.progressive_metadata.start_indexing(
        indexer.embedding_provider.get_provider_name(),
        indexer.embedding_provider.get_current_model(),
        git_status,
    )
    indexer.progressive_metadata.set_files_to_index(files)
    indexer.progressive_metadata.mark_file_completed(str(files[0]), chunks_count=1)
    indexer.progressive_metadata.mark_file_completed(str(files[1]), chunks_count=1)


def _assert_precondition_mtime_scan_would_miss_untouched_files(
    indexer: SmartIndexer, files: List[Path]
) -> None:
    """Sanity check that the reproduction setup is actually load-bearing:
    the resume_timestamp derived from the interrupted run's
    last_index_timestamp must be AFTER the untouched files' deterministic
    past mtime, so find_modified_files() would genuinely exclude them."""
    resume_timestamp = indexer.progressive_metadata.get_resume_timestamp(
        _SAFETY_BUFFER_SECONDS
    )
    assert resume_timestamp > 0.0
    for f in files[2:]:
        assert f.stat().st_mtime < resume_timestamp, (
            "test setup invalid: untouched file's mtime must be older than "
            "resume_timestamp for this reproduction to be load-bearing"
        )


class TestInterruptedRunTrustResumeStateFalseReconciles:
    """trust_resume_state=False after a genuinely
    interrupted run must not silently stall forever."""

    def test_three_consecutive_trust_false_runs_after_interruption(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        files = _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        _simulate_interrupted_run_after_two_files(indexer, files)
        _assert_precondition_mtime_scan_would_miss_untouched_files(indexer, files)

        # Fixed behavior: the FIRST trust_resume_state=False run after a
        # genuinely interrupted operation must index all 5 files (a fresh
        # reconcile-style disk-vs-database walk, not the mtime scan) and
        # leave the operation correctly marked completed.
        stats = indexer.smart_index(force_full=False, trust_resume_state=False)

        assert stats.files_processed == 5, (
            "SECURITY/CORRECTNESS: after an interrupted run, "
            "trust_resume_state=False must not silently skip the untouched "
            f"files forever. Got files_processed={stats.files_processed}"
        )
        assert indexer.progressive_metadata.metadata["status"] == "completed", (
            "Expected the operation to be left in a correct 'completed' "
            f"state, got: {indexer.progressive_metadata.metadata['status']}"
        )

        # A subsequent trust_resume_state=False run must find nothing left
        # to do (already fully reconciled) -- not fail, not re-stall.
        stats_again = indexer.smart_index(force_full=False, trust_resume_state=False)
        assert stats_again.files_processed == 0
        assert indexer.progressive_metadata.metadata["status"] == "completed"

        # A THIRD consecutive trust_resume_state=False run must behave
        # identically -- reconcile's "nothing to do" early return must mark
        # completion every time it takes that path, not just once, or a
        # status regression on run 2 would silently resurface on run 3.
        stats_third = indexer.smart_index(force_full=False, trust_resume_state=False)
        assert stats_third.files_processed == 0
        assert indexer.progressive_metadata.metadata["status"] == "completed"

    def test_reconcile_with_nothing_to_do_still_marks_completed_across_three_runs(
        self, tmp_path: Path
    ) -> None:
        """Discriminating regression for reconcile's early-return gap: the
        "nothing to reconcile" branch inside ``_do_reconcile_with_database``
        must mark the operation completed, not leave status wherever a
        PRIOR run happened to leave it. Reproduces the genuine Bug
        #1218-class crash window: a run that finishes writing every file's
        vectors but crashes before flipping status to "completed" -- so
        status is STILL "in_progress" the very first time
        trust_resume_state=False routes into a reconcile that finds
        genuinely nothing to do."""
        repo = tmp_path / "repo"
        _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        # Real full index: writes every file's vectors for real, then marks
        # status "completed" for real.
        first_stats = indexer.smart_index(force_full=True)
        assert first_stats.files_processed == 5
        assert indexer.progressive_metadata.metadata["status"] == "completed"

        # Simulate the crash window: status regresses to "in_progress" (a
        # crash between the last vector write and complete_indexing())
        # while disk and database are ALREADY fully reconciled -- reconcile
        # finds nothing to do on the very FIRST fallback run.
        indexer.progressive_metadata.metadata["status"] = "in_progress"
        indexer.progressive_metadata._save_metadata()

        for run_number in (1, 2, 3):
            stats = indexer.smart_index(force_full=False, trust_resume_state=False)
            assert stats.files_processed == 0, (
                f"run {run_number}: disk and database were already fully "
                f"reconciled, expected 0 files processed, got "
                f"{stats.files_processed}"
            )
            assert indexer.progressive_metadata.metadata["status"] == "completed", (
                f"run {run_number}: CORRECTNESS regression (Bug #1218-class): "
                "reconcile found nothing to do but left status="
                f"{indexer.progressive_metadata.metadata['status']!r} instead "
                "of marking the operation completed -- every subsequent "
                "trust_resume_state=False run would re-run a full reconcile "
                "forever."
            )
