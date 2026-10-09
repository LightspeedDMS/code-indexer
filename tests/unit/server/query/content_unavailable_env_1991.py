"""Shared environment for the Bug #1991 content-unavailable front-door tests.

A real git repository with a real ``.code-indexer/config.json``, chunked by
the real FixedSizeChunker and indexed into a real FilesystemVectorStore.
``Broken.cs`` is then made unreadable on BOTH retrieval tiers (its working
path becomes a directory and its git blob object is deleted), while
``good.py`` stays readable.

Only the embedding provider -- an external network service -- is replaced,
by a deterministic in-process implementation of the real interface.
"""

from __future__ import annotations

import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import numpy as np

from code_indexer.config import Config, ConfigManager, IndexingConfig
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8
COLLECTION = "voyage-code-3"
REPO_ALIAS = "example-repo"
GOOD_FILE = "good.py"
BROKEN_FILE = "Broken.cs"
UNAVAILABLE_MARKER = "[content unavailable: file could not be read]"


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, network-free EmbeddingProvider."""

    def __init__(self, console: Any = None) -> None:
        super().__init__(console)

    def _vector_for(self, text: str) -> List[float]:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        vec = np.random.default_rng(seed).random(VECTOR_DIM).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()  # type: ignore[no-any-return]

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return self._vector_for(text)

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: Any = None,
        retry: bool = True,
    ) -> List[List[float]]:
        return [self._vector_for(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None, *, embedding_purpose: Any = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=self._vector_for(text),
            model=COLLECTION,
            tokens_used=len(text.split()),
            provider="fake-voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: Any = None,
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector_for(t) for t in texts],
            model=COLLECTION,
            total_tokens_used=sum(len(t.split()) for t in texts),
            provider="fake-voyage-ai",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"name": COLLECTION, "dimensions": VECTOR_DIM}

    def get_provider_name(self) -> str:
        return "voyage-ai"

    def get_current_model(self) -> str:
        return COLLECTION

    def supports_batch_processing(self) -> bool:
        return True


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def build_indexed_repo(root: Path) -> Path:
    """Index good.py and Broken.cs for real, then break Broken.cs."""
    repo = root / REPO_ALIAS
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / GOOD_FILE).write_text("def image_generator():\n    return 'thumb'\n")
    (repo / BROKEN_FILE).write_bytes(
        "/// Caf\xe9 image generator\nclass G { void Thumb() {} }\n".encode("latin-1")
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    ConfigManager(repo / ".code-indexer" / "config.json").save(
        Config(codebase_dir=repo)
    )

    provider = FakeEmbeddingProvider()
    chunker = FixedSizeChunker(IndexingConfig())
    store = FilesystemVectorStore(
        base_path=repo / ".code-indexer" / "index", project_root=repo
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    points = []
    for rel in (GOOD_FILE, BROKEN_FILE):
        for i, chunk in enumerate(chunker.chunk_file(repo / rel, repo_root=repo)):
            points.append(
                {
                    "id": f"{rel}-{i}",
                    "vector": provider.get_embedding(chunk["text"]),
                    "payload": {
                        "path": rel,
                        "line_start": chunk["line_start"],
                        "line_end": chunk["line_end"],
                        "content": chunk["text"],
                        "language": "py" if rel.endswith(".py") else "cs",
                        "type": "content",
                    },
                }
            )
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, points)
    store.end_indexing(COLLECTION)

    blob = _git(repo, "rev-parse", f"HEAD:{BROKEN_FILE}")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    (repo / BROKEN_FILE).unlink()
    (repo / BROKEN_FILE).mkdir()
    return repo


@contextmanager
def real_store_search() -> Iterator[FakeEmbeddingProvider]:
    """Serve server-side semantic search from the real on-disk index; only
    the embedding-provider factory (external service) is replaced."""
    from code_indexer.server.fault_injection.null_factory import NullFaultFactory
    import code_indexer.server.app as app_module

    provider = FakeEmbeddingProvider()
    had_factory = hasattr(app_module.app.state, "http_client_factory")
    original_factory = getattr(app_module.app.state, "http_client_factory", None)
    app_module.app.state.http_client_factory = NullFaultFactory()
    try:
        with (
            patch(
                "code_indexer.server.services.search_service."
                "EmbeddingProviderFactory.create",
                return_value=provider,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
        ):
            yield provider
    finally:
        if had_factory:
            app_module.app.state.http_client_factory = original_factory
        else:
            del app_module.app.state.http_client_factory


def by_path(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index result rows by file path (each test file has one chunk)."""
    return {row["file_path"]: row for row in rows}
