"""#2047 (S21): daemon mode applies ``file_extensions`` with the same rule
and the same shared vector-store condition as the CLI and the server.

Real CIDXDaemonService search methods over a real git repo with real
FilesystemVectorStore and Tantivy indexes; only the embedding provider
(external service) is replaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from tests.unit.server.query.extension_filter_env_2047 import (
    CORPUS,
    LIMIT,
    QUERY,
    RankedEmbeddingProvider,
    build_corpus_repo,
    expected_fts,
    passes,
)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    yield build_corpus_repo(tmp_path_factory.mktemp("daemon-ext-2047"))


def test_daemon_fts_applies_file_extensions(repo: Path) -> None:
    from code_indexer.daemon.service import CIDXDaemonService

    got = CIDXDaemonService()._execute_fts_search(
        str(repo), QUERY, limit=LIMIT, file_extensions=["MD"]
    )
    assert [r["path"] for r in got] == expected_fts(repo, ["MD"], None)


def test_daemon_selective_language_filter_fills_the_limit(repo: Path) -> None:
    """A filtered semantic query searches the shared filtered candidate
    window: the Python files rank below 19 others, yet the limit fills."""
    from code_indexer.daemon.service import CIDXDaemonService

    python_by_rank = [
        p for p, _ in sorted(CORPUS, key=lambda e: e[1]) if p.endswith(".py")
    ]
    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
        return_value=RankedEmbeddingProvider(),
    ):
        results, _ = CIDXDaemonService()._execute_semantic_search(
            str(repo), QUERY, limit=LIMIT, languages=["python"]
        )
    assert [r["payload"]["path"] for r in results] == python_by_rank[:LIMIT]


def test_daemon_semantic_pushes_file_extensions_into_the_store(repo: Path) -> None:
    from code_indexer.daemon.service import CIDXDaemonService

    values = [".PY", "md"]
    # HNSW window (2 x limit) covers the whole corpus: the store's filter,
    # not the window, decides which files come back.
    full_window_limit = (len(CORPUS) + 1) // 2
    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
        return_value=RankedEmbeddingProvider(),
    ):
        results, _ = CIDXDaemonService()._execute_semantic_search(
            str(repo), QUERY, limit=full_window_limit, file_extensions=values
        )

    expected = [
        p for p, _ in sorted(CORPUS, key=lambda e: e[1]) if passes(p, values, None)
    ]
    assert [r["payload"]["path"] for r in results] == expected
