"""Indexer resume state:
a reconcile run where every file's analysis fails must not be
falsely marked completed.

``_do_reconcile_with_database()``'s per-file loop wraps content-id
analysis in a broad ``except Exception: continue`` -- a file whose
analysis throws is silently skipped, never added to ``files_to_index``.
When EVERY file's analysis fails this way, ``files_to_index`` ends up
empty for reasons that have nothing to do with the files actually being
up-to-date, yet the "nothing to reconcile" early return unconditionally
calls ``complete_indexing()``. That is a false completion (Bug
#1218-class silent partial index): the operation is marked done while no
file was ever actually verified.

This test drives the REAL ``SmartIndexer.smart_index(reconcile_with_database=True)``
entry point against a real git repository and a real
``FilesystemVectorStore``, forcing every file's content-id analysis to
raise, and asserts the operation is left NOT completed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

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


def _deterministic_embedding(text: str) -> List[float]:
    """Real (non-mocked), deterministic local embedding: no network call."""
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / _BYTE_MAX_VALUE) * _VECTOR_SCALE - _VECTOR_OFFSET
        for i in range(_VECTOR_DIM)
    ]


class _DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working EmbeddingProvider (no mocking) -- local
    duplicate of the identical helper used by the other resume-state
    reconcile tests, kept local per this project's own precedent rather
    than a cross-test-module import."""

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
    config = Config(codebase_dir=repo)
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
    """Real git repo with 5 committed files, each with unique content."""
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
    return files


class TestReconcileAnalysisFailureNoFalseCompletion:
    """Reconcile must not mark the operation completed
    when it found nothing to index ONLY because every file's analysis
    failed."""

    def test_all_files_failing_analysis_does_not_mark_completed(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        # Simulate a crash/stale state: status is "in_progress" going into
        # this reconcile run, and must stay that way if the run genuinely
        # verified nothing.
        indexer.progressive_metadata.metadata["status"] = "in_progress"
        indexer.progressive_metadata._save_metadata()

        with patch.object(
            indexer,
            "_get_effective_content_id_for_reconcile",
            side_effect=RuntimeError("simulated analysis failure"),
        ):
            stats = indexer.smart_index(force_full=False, reconcile_with_database=True)

        assert stats.files_processed == 0, (
            "No file should have been queued for indexing when every "
            f"file's analysis raised. Got files_processed={stats.files_processed}"
        )
        assert indexer.progressive_metadata.metadata["status"] != "completed", (
            "REGRESSION: reconcile found nothing to index because "
            "EVERY file's analysis failed, not because files were "
            "genuinely up-to-date -- marking the operation 'completed' "
            "here is a false completion. Got status="
            f"{indexer.progressive_metadata.metadata['status']!r}"
        )
