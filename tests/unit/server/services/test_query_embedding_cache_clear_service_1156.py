"""Story #1156: QueryEmbeddingCache.clear_all() service-level tests.

AC3: clear_all() deletes all rows from the persisted table AND resets
the in-process count memo (_cached_total) to 0 so the ObservableGauge
and Web UI count readout are accurate immediately.

Tests use a real SQLite backend (no mocks) consistent with the project's
anti-mock principle.
"""

from __future__ import annotations

import struct
import tempfile
import time
from typing import Any


def _make_embedding(dim: int = 4) -> bytes:
    return struct.pack(f"<{dim}f", *[float(i) for i in range(dim)])


def _make_cache_with_sqlite(db_path: str, max_entries: int = 1000) -> Any:
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )
    from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache

    backend = QueryEmbeddingCacheSqliteBackend(db_path)
    return QueryEmbeddingCache(
        backend=backend,
        enabled=True,
        voyage_mode="on",
        cohere_mode="on",
        max_entries=max_entries,
    )


def _insert(cache: Any, key: str = "k1", dimension: int = 4) -> None:
    """Directly upsert a row via the backend to simulate cached entries."""
    now = time.time()
    cache._backend.upsert(
        cache_key=key,
        provider="voyage-ai",
        model="voyage-code-3",
        dimension=dimension,
        embedding=_make_embedding(dimension),
        created_at=now,
        last_used=now,
    )


class TestQueryEmbeddingCacheClearAll:
    def test_clear_all_resets_cached_total(self) -> None:
        """After clear_all(), cached_total_entries() returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = _make_cache_with_sqlite(f"{tmpdir}/qec.db")
            # Manually set the memo to simulate prior upserts.
            cache._cached_total = 5

            cache.clear_all()

            assert cache.cached_total_entries() == 0

    def test_clear_all_empties_backend_table(self) -> None:
        """After clear_all(), total_entries() (live DB count) returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = _make_cache_with_sqlite(f"{tmpdir}/qec.db")
            _insert(cache, "k1")
            _insert(cache, "k2")
            assert cache.total_entries() == 2

            cache.clear_all()

            assert cache.total_entries() == 0

    def test_clear_all_on_empty_is_noop_success(self) -> None:
        """Clearing an already-empty cache does not raise (AC7)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = _make_cache_with_sqlite(f"{tmpdir}/qec.db")
            assert cache.total_entries() == 0
            assert cache.cached_total_entries() == 0

            # Must not raise
            cache.clear_all()

            assert cache.total_entries() == 0
            assert cache.cached_total_entries() == 0

    def test_clear_all_resets_nonzero_memo_to_zero(self) -> None:
        """_cached_total is reset to 0 even if it was > 0 before clear."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = _make_cache_with_sqlite(f"{tmpdir}/qec.db")
            cache._cached_total = 42

            cache.clear_all()

            assert cache._cached_total == 0

    def test_clear_all_after_populate_then_reinsert(self) -> None:
        """After clear_all(), new inserts are counted from 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = _make_cache_with_sqlite(f"{tmpdir}/qec.db")
            _insert(cache, "k1")
            _insert(cache, "k2")
            cache.clear_all()
            assert cache.total_entries() == 0

            # Re-insert one row
            _insert(cache, "k3")
            assert cache.total_entries() == 1
