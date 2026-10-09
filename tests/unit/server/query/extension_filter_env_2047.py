"""Shared environment for the #2047 (S21) ``file_extensions`` tests.

An exhaustive small corpus -- ``.js`` noise, ``.txt``, extensionless files,
``.md``/``.MD`` and ``.py``/``.PY`` -- in a real git repository, indexed into a
real FilesystemVectorStore (one chunk per file) and a real Tantivy FTS index
whose documents are built exactly as the indexer builds them (``language`` is
the case-preserved suffix, ``txt`` for an extensionless file).

Only the embedding provider -- an external network service -- is replaced, by
a deterministic in-process implementation of the real interface whose vector
for a chunk is fixed by the ``rankNN`` token in its text: the higher NN, the
farther the chunk from every query. The ``.js`` noise ranks first in both
semantic and full-text order, so every filtered query must reach past the
first ``limit`` candidates to fill its answer.

The expected answers are computed by brute force over every chunk (semantic:
cosine against every stored vector; FTS: one unfiltered full scan of the
index), then filtered by an independent implementation of the S21 rule.
"""

from __future__ import annotations

import math
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from unittest.mock import patch

from code_indexer.config import Config, ConfigManager
from code_indexer.services.language_mapper import LanguageMapper
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.unit.server.query.content_unavailable_env_1991 import (
    COLLECTION,
    VECTOR_DIM,
    FakeEmbeddingProvider,
)

REPO_ALIAS = "example-ext-repo"
QUERY = "widget"
LIMIT = 3
RRF_K = 60  # the hybrid merge's reciprocal-rank-fusion constant
_RANK_RE = re.compile(r"rank(\d+)")
_ANGLE_STEP = 0.05

# (path, rank). Rank 1 is nearest to the query. Extensionless files (stored
# as "txt" in FTS) outrank the .txt files, so a "txt" filter must drop them;
# .md and .py interleave so a multi-extension filter must return both kinds.
CORPUS: List[Tuple[str, int]] = (
    [(f"src/noise_{i:02d}.js", i) for i in range(1, 11)]
    + [("Makefile", 11), ("LICENSE", 12), ("tools/Dockerfile", 13)]
    + [(f"notes/n{i}.txt", 13 + i) for i in range(1, 5)]
    + [
        ("docs/a.md", 18),
        ("pkg/m7.Py", 19),  # mixed case: only a case-insensitive match finds it
        ("pkg/m1.py", 20),
        ("docs/b.md", 21),
        ("pkg/M2.PY", 22),
        ("docs/c.md", 23),
        ("pkg/m3.py", 24),
        ("docs/d.md", 25),
        ("pkg/M4.PY", 26),
        ("docs/E.MD", 27),
        ("pkg/m5.py", 28),
        ("pkg/m6.py", 29),
    ]
)


def file_text(path: str, rank: int) -> str:
    """One-line file body; .js noise repeats the query term (FTS-first).

    ``rank`` filler words make every body a different length, so every BM25
    score differs (longer = lower) and the full-text order is unambiguous.
    """
    hits = "widget widget widget" if path.endswith(".js") else "widget"
    return f"{hits} rank{rank:02d} {'pad ' * rank}body\n"


class RankedEmbeddingProvider(FakeEmbeddingProvider):
    """Vector = unit vector at angle rank * step; no rank token = angle 0."""

    def _vector_for(self, text: str) -> List[float]:
        found = _RANK_RE.search(text)
        angle = int(found.group(1)) * _ANGLE_STEP if found else 0.0
        return [math.cos(angle), math.sin(angle)] + [0.0] * (VECTOR_DIM - 2)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _fts_dir(repo: Path) -> Path:
    return repo / ".code-indexer" / "tantivy_index"


def build_corpus_repo(root: Path) -> Path:
    """Write, commit and index (semantic + FTS) every corpus file."""
    repo = root / REPO_ALIAS
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for rel, rank in CORPUS:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(file_text(rel, rank))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    ConfigManager(repo / ".code-indexer" / "config.json").save(
        Config(codebase_dir=repo)
    )

    provider = RankedEmbeddingProvider()
    store = FilesystemVectorStore(
        base_path=repo / ".code-indexer" / "index", project_root=repo
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    fts = TantivyIndexManager(_fts_dir(repo))
    fts.initialize_index(create_new=True)
    points = []
    try:
        for i, (rel, rank) in enumerate(CORPUS):
            text = file_text(rel, rank)
            suffix = PurePosixPath(rel).suffix.lstrip(".")
            points.append(
                {
                    "id": f"chunk-{i}",
                    "vector": provider.get_embedding(text),
                    "payload": {
                        "path": rel,
                        "line_start": 1,
                        "line_end": 1,
                        "content": text,
                        "language": suffix,
                        "type": "content",
                        # Written by git-aware indexing; the CLI filters on it.
                        "git_available": True,
                    },
                }
            )
            fts.add_document(
                {
                    "path": rel,
                    "content": text,
                    "content_raw": text,
                    "identifiers": text.split(),
                    "line_start": 1,
                    "line_end": 1,
                    "language": suffix or "txt",
                }
            )
        fts.commit()
    finally:
        fts.close()
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, points)
    store.end_indexing(COLLECTION)
    return repo


@contextmanager
def ranked_store_search() -> Iterator[RankedEmbeddingProvider]:
    """Serve server-side semantic search from the real on-disk index; only
    the embedding-provider factory (external service) is replaced."""
    from code_indexer.server.fault_injection.null_factory import NullFaultFactory
    import code_indexer.server.app as app_module

    provider = RankedEmbeddingProvider()
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


# ---------------------------------------------------------------- oracles


def rule_extensions(values: Sequence[str]) -> Set[str]:
    """The S21 rule, written independently: strip, lowercase, one dot."""
    out = set()
    for value in values:
        value = value.strip().lower()
        out.add(value[1:] if value.startswith(".") else value)
    return out


def passes(path: str, values: Sequence[str], language: Optional[str]) -> bool:
    suffix = PurePosixPath(path).suffix
    if not suffix or suffix[1:].lower() not in rule_extensions(values):
        return False
    if language is None:
        return True
    # The language filter keeps its own (case-preserving) semantics.
    return suffix[1:] in LanguageMapper().get_extensions(language)


def expected_semantic(
    values: Sequence[str], language: Optional[str], limit: int = LIMIT
) -> List[str]:
    provider = RankedEmbeddingProvider()
    query = provider.get_embedding(QUERY)

    def similarity(item: Tuple[str, int]) -> float:
        vec = provider.get_embedding(file_text(*item))
        return sum(a * b for a, b in zip(query, vec))

    ranked = sorted(CORPUS, key=similarity, reverse=True)
    return [p for p, _ in ranked if passes(p, values, language)][:limit]


def expected_fts(
    repo: Path, values: Sequence[str], language: Optional[str], limit: int = LIMIT
) -> List[str]:
    fts = TantivyIndexManager(_fts_dir(repo))
    fts.open_for_search()
    try:
        hits = list(fts.search(QUERY, limit=0, snippet_lines=0))
    finally:
        fts.close()
    scan = [r["path"] for r in hits]
    assert len(scan) == len(CORPUS), scan
    scores = [r["score"] for r in hits]
    assert len(set(scores)) == len(scores), "tied BM25 scores: ambiguous oracle"
    return [p for p in scan if passes(p, values, language)][:limit]


def expected_hybrid(
    fts: List[str], semantic: List[str], limit: int = LIMIT
) -> Tuple[Set[str], bool]:
    """Reciprocal-rank fusion of the two filtered lists. Returns the
    top-``limit`` set and whether it is unambiguous (no tie at the cut)."""
    scores: Dict[str, float] = {}
    for ranked in (fts, semantic):
        for rank, path in enumerate(ranked, start=1):
            scores[path] = scores.get(path, 0.0) + 1.0 / (RRF_K + rank)
    order = sorted(scores, key=lambda p: scores[p], reverse=True)
    unambiguous = len(order) <= limit or (
        scores[order[limit - 1]] != scores[order[limit]]
    )
    return set(order[:limit]), unambiguous


def paths(rows: List[Dict[str, Any]], key: str = "file_path") -> List[str]:
    return [str(row[key]) for row in rows]


@contextmanager
def corpus_app(root: Path) -> Iterator[Any]:
    """A real isolated app (see tests/unit/server/_isolated_app.py) with no
    provider keys in the environment (deterministic primary-only routing)."""
    import pytest

    from tests.unit.server._isolated_app import isolated_app

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("CO_API_KEY", raising=False)
        mp.delenv("VOYAGE_API_KEY", raising=False)
        with isolated_app(root) as app:
            yield app
