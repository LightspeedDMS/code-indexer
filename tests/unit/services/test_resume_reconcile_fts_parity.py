"""Reconcile FTS-lost regression.

``_do_reconcile_with_database()`` hardcoded a LOCAL
``fts_manager: Optional[TantivyIndexManager] = None`` instead of accepting
the already-initialized ``fts_manager`` ``smart_index()`` built when
``enable_fts=True``. Since the ``trust_resume_state=False``
interrupted-fallback (see
``test_resume_interrupted_reconcile_fallback.py``) routes through
``_do_reconcile_with_database()``, an interrupted ``--fts`` server-spawned
run silently lost every FTS update for the files that reconcile picked up
-- semantic search kept working, full-text search did not.

Fix: ``_do_reconcile_with_database()`` now accepts an ``fts_manager``
parameter and both ``smart_index()`` call sites pass the already-built
manager through, instead of it being hardcoded to ``None``.

This test drives the REAL ``SmartIndexer.smart_index()`` entry point with
a real git repo, a real ``FilesystemVectorStore``, and a real
``TantivyIndexManager`` FTS index on disk (no mocking of the code under
test) -- and proves the missed file lands in BOTH the semantic index
(``files_processed``) and the FTS index (a fresh, independent
``TantivyIndexManager`` opened read-only against the same on-disk index
directory afterward finds the new file's path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
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
    duplicate of the identical helper in
    test_resume_interrupted_reconcile_fallback.py, kept local per this
    project's own precedent rather than a cross-test-module import."""

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


class TestInterruptedReconcileFallbackFeedsFtsIndexToo:
    """The trust_resume_state=False reconcile fallback must
    feed BOTH the semantic index and the FTS index, not just the
    semantic one."""

    def test_interrupted_fts_run_indexes_missed_file_into_fts_and_semantic(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        # Baseline: a normal full index WITH FTS enabled.
        baseline_stats = indexer.smart_index(force_full=True, enable_fts=True)
        assert baseline_stats.files_processed == 5
        assert indexer.progressive_metadata.metadata["status"] == "completed"

        # A new file appears on disk AFTER the baseline index -- exactly
        # what the interrupted-fallback reconcile must pick up.
        missed_file = repo / "f.py"
        missed_file.write_text("# file f unique FTS marker\n")

        # Simulate a stale interrupted resume state left behind by a
        # crash, forcing the trust_resume_state=False fallback to route
        # through _do_reconcile_with_database().
        indexer.progressive_metadata.metadata["status"] = "in_progress"

        stats = indexer.smart_index(
            force_full=False, trust_resume_state=False, enable_fts=True
        )

        assert stats.files_processed >= 1, (
            "The reconcile fallback's semantic-side (vector store) "
            "indexing must pick up the new file on disk. "
            f"Got files_processed={stats.files_processed}"
        )

        # FTS side: open a FRESH, independent TantivyIndexManager against
        # the SAME on-disk index directory (never reusing the in-process
        # instance smart_index() built internally) to prove the missed
        # file's FTS document was actually committed to disk.
        fts_index_dir = repo / ".code-indexer" / "tantivy_index"
        fts_reader = TantivyIndexManager(fts_index_dir)
        fts_reader.open_for_search()
        indexed_paths = fts_reader.get_all_indexed_paths()

        assert "f.py" in indexed_paths, (
            "The trust_resume_state=False reconcile fallback must feed "
            "the FTS index too, not just the semantic index. FTS-indexed "
            f"paths: {indexed_paths}"
        )
