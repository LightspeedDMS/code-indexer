"""--clear precedence regression.

``smart_index()`` has a ``trust_resume_state=False`` fallback: when a stale
``in_progress``/``failed`` resume state is present and resume state must
not be trusted, indexing routes to ``_do_reconcile_with_database`` instead
of the mtime-scan incremental path (see
``test_resume_interrupted_reconcile_fallback.py``).

That fallback's guard forgot to exclude ``force_full=True`` (``--clear``):
a caller that explicitly asks for a full clear+reindex, on a repo that
happens to carry a stale interrupted resume state, was silently redirected
to the RECONCILE branch instead of ``_do_full_index()`` -- so
``vector_store_client.clear_collection()`` was never called and only the
reconcile-style disk/db diff ran (which can decide there is nothing to do
at all, e.g. when no file content changed since the last completed index).
This defeats the entire purpose of ``--clear``: old collection content is
never actually cleared.

Fix: the fallback must only apply ``if not trust_resume_state and not
force_full and (...)`` -- ``force_full=True`` always goes straight to
``_do_full_index()``, regardless of resume-state trust.

This test drives the REAL ``SmartIndexer.smart_index()`` entry point
against a real git repo and a real ``FilesystemVectorStore`` (no mocking
of the code under test); only ``clear_collection`` is wrapped with a
call-through spy so the test can observe whether the real clear actually
happened, without replacing its real behavior.
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


class TestClearForceFullNotOverriddenByInterruptedReconcileFallback:
    """--clear (force_full=True) must still fully clear +
    reindex even when a stale in_progress/failed resume state is present,
    never silently downgraded to the trust_resume_state reconcile
    fallback."""

    def test_force_full_with_interrupted_state_clears_collection_and_full_indexes(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        files = _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        # Baseline: a normal, uninterrupted full index.
        baseline_stats = indexer.smart_index(force_full=True)
        assert baseline_stats.files_processed == 5
        assert indexer.progressive_metadata.metadata["status"] == "completed"

        # Simulate a stale/interrupted resume state left behind by a crash
        # (or a repository-authored metadata file) -- content on disk is
        # UNCHANGED since the baseline index, so a reconcile diff would
        # find nothing to do at all.
        indexer.progressive_metadata.metadata["status"] = "in_progress"
        indexer.progressive_metadata.metadata["files_to_index"] = [
            str(files[0]),
            str(files[1]),
        ]
        indexer.progressive_metadata.metadata["current_file_index"] = 0

        clear_calls: List[str] = []
        real_clear_collection = indexer.vector_store_client.clear_collection

        def _spy_clear_collection(collection_name: str) -> None:
            clear_calls.append(collection_name)
            return real_clear_collection(collection_name)

        indexer.vector_store_client.clear_collection = _spy_clear_collection  # type: ignore[method-assign]

        stats = indexer.smart_index(force_full=True, trust_resume_state=False)

        assert clear_calls, (
            "--clear (force_full=True) with a stale in_progress/failed "
            "resume state must still route to _do_full_index() and call "
            "clear_collection() -- it must never be silently intercepted "
            "by the trust_resume_state reconcile fallback."
        )
        assert stats.files_processed == 5, (
            "force_full=True must perform a FULL index of every file on "
            "disk, not a partial reconcile diff (which would find nothing "
            f"changed and process 0 files). Got files_processed={stats.files_processed}"
        )
        assert indexer.progressive_metadata.metadata["status"] == "completed"

    def test_force_full_alone_unaffected_by_this_guard(self, tmp_path: Path) -> None:
        """Sanity: plain force_full=True (trust_resume_state default True)
        with no interrupted state must still behave exactly as before --
        this guards against an overcorrection that disables --clear
        entirely."""
        repo = tmp_path / "repo"
        _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        stats = indexer.smart_index(force_full=True)
        assert stats.files_processed == 5
        assert indexer.progressive_metadata.metadata["status"] == "completed"
