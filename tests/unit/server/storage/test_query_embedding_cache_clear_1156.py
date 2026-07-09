"""Story #1156: clear_all() tests for both QueryEmbeddingCache backends.

Covers AC3: a new clear_all() method on both QueryEmbeddingCacheSqliteBackend
and QueryEmbeddingCachePostgresBackend that deletes all rows from the
query_embedding_cache table.

Tests:
- SQLite: populate table, call clear_all(), assert table empty + count 0.
- SQLite: clear on empty table is a no-op success (AC7).
- PG: clear_all() delegates to clear() which executes DELETE FROM.
- PG: clear_all() on an empty table is a no-op success.
"""

from __future__ import annotations

import struct
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# SQLite backend tests — real SQLite, real table
# ---------------------------------------------------------------------------


def _make_sqlite_backend(db_path: str) -> Any:
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    return QueryEmbeddingCacheSqliteBackend(db_path)


def _make_embedding(dim: int = 4, seed: float = 1.0) -> bytes:
    """Return a minimal float32 LE blob of dimension dim."""
    return struct.pack(f"<{dim}f", *[seed + i * 0.1 for i in range(dim)])


def _insert_row(backend: Any, key: str = "k1") -> None:
    now = time.time()
    backend.upsert(
        cache_key=key,
        provider="voyage-ai",
        model="voyage-code-3",
        dimension=4,
        embedding=_make_embedding(4),
        created_at=now,
        last_used=now,
    )


class TestQueryEmbeddingCacheSqliteBackendClearAll:
    def test_clear_all_empties_table(self) -> None:
        """After clear_all(), total_entries() returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_sqlite_backend(f"{tmpdir}/qec.db")
            _insert_row(backend, "k1")
            _insert_row(backend, "k2")
            assert backend.total_entries() == 2

            backend.clear_all()

            assert backend.total_entries() == 0

    def test_clear_all_on_empty_table_is_noop(self) -> None:
        """Clearing an already-empty table succeeds without error (AC7)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_sqlite_backend(f"{tmpdir}/qec.db")
            assert backend.total_entries() == 0

            # Must not raise
            backend.clear_all()

            assert backend.total_entries() == 0

    def test_clear_all_removes_all_rows(self) -> None:
        """All rows are deleted, not just some."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_sqlite_backend(f"{tmpdir}/qec.db")
            for i in range(10):
                _insert_row(backend, f"key_{i}")
            assert backend.total_entries() == 10

            backend.clear_all()

            assert backend.total_entries() == 0
            # lookup should also return None
            result = backend.lookup("key_0", "voyage-ai", "voyage-code-3", 4)
            assert result is None

    def test_clear_all_delegates_to_clear(self) -> None:
        """clear_all() delegates to clear() — both leave table empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_sqlite_backend(f"{tmpdir}/qec.db")
            _insert_row(backend, "x")
            backend.clear_all()
            assert backend.total_entries() == 0


# ---------------------------------------------------------------------------
# PG backend tests — mock pool (mirrors test_query_embedding_cache_backend_1105.py)
# ---------------------------------------------------------------------------


def _make_mock_pool(fetchone_return: Any = None, rowcount: int = 0) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = []
    cursor.rowcount = rowcount
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> Any:
    return pool.connection.return_value.__enter__.return_value


def _make_pg_backend(pool: MagicMock) -> Any:
    from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
        QueryEmbeddingCachePostgresBackend,
    )

    backend = QueryEmbeddingCachePostgresBackend(pool)
    # Clear schema-setup calls so per-test assertions are clean.
    _get_conn(pool).reset_mock()
    return backend


class TestQueryEmbeddingCachePostgresBackendClearAll:
    def test_clear_all_executes_delete(self) -> None:
        """clear_all() calls DELETE FROM query_embedding_cache."""
        pool = _make_mock_pool()
        backend = _make_pg_backend(pool)

        backend.clear_all()

        conn = _get_conn(pool)
        executed_sql = conn.execute.call_args[0][0]
        assert "DELETE FROM query_embedding_cache" in executed_sql

    def test_clear_all_commits(self) -> None:
        """clear_all() commits the transaction."""
        pool = _make_mock_pool()
        backend = _make_pg_backend(pool)

        backend.clear_all()

        conn = _get_conn(pool)
        conn.commit.assert_called_once()

    def test_clear_all_on_empty_is_noop(self) -> None:
        """clear_all() on empty table succeeds (AC7)."""
        pool = _make_mock_pool(fetchone_return=(0,))
        backend = _make_pg_backend(pool)

        # Must not raise
        backend.clear_all()

        conn = _get_conn(pool)
        conn.execute.assert_called()

    def test_clear_all_fail_open(self) -> None:
        """clear_all() swallows pool exceptions (fail-open pattern)."""
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("pool unavailable")

        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool

        # Must not raise
        backend.clear_all()
