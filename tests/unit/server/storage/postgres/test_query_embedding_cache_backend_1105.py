"""Unit tests for QueryEmbeddingCachePostgresBackend (Story #1105, N2).

Uses a MagicMock connection pool — no real PostgreSQL required.
Mirrors the pattern from test_xray_cache_backend.py.

Coverage goals:
- Schema creation on __init__ (CREATE TABLE + index).
- lookup() returns None on miss, bytes on hit.
- upsert() calls INSERT … ON CONFLICT … DO UPDATE with correct params.
- touch_last_used() calls UPDATE SET last_used.
- prune_to_max() deletes excess rows ordered by last_used ASC.
- total_entries() returns COUNT(*).
- clear() executes DELETE FROM.
- Every method is fail-open: a pool error logs a WARNING and returns the
  safe default (None / 0 / no-op) without raising.

Note: SQL-construction / round-trip logic is fully exercised here.
A real-PG integration test (restart-warmth, actual persistence) is gated
on a live PostgreSQL cluster and belongs in the staging E2E suite (AC4).
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import numpy as np


# ---------------------------------------------------------------------------
# Mock pool helpers (identical pattern to test_xray_cache_backend.py)
# ---------------------------------------------------------------------------


def _make_mock_pool(fetchone_return: Any = None, rowcount: int = 0) -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
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


def _make_backend(pool: MagicMock) -> Any:
    from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
        QueryEmbeddingCachePostgresBackend,
    )

    backend = QueryEmbeddingCachePostgresBackend(pool)
    # Clear the schema-setup calls so per-test assertions are clean
    _get_conn(pool).reset_mock()
    return backend


def _encode_vec(vec: list) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    def test_schema_created_on_init(self) -> None:
        """__init__ must call CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS."""
        pool = _make_mock_pool()
        conn = _get_conn(pool)
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        QueryEmbeddingCachePostgresBackend(pool)
        calls = conn.execute.call_args_list
        # Two execute calls: CREATE TABLE + CREATE INDEX
        assert len(calls) >= 2
        first_sql = str(calls[0])
        second_sql = str(calls[1])
        assert "query_embedding_cache" in first_sql
        assert "CREATE TABLE IF NOT EXISTS" in first_sql
        assert "idx_qec_last_used" in second_sql

    def test_schema_creation_fail_open(self) -> None:
        """Schema setup failure must NOT raise — backend still constructs."""
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        # Must not raise
        backend = QueryEmbeddingCachePostgresBackend(pool)
        assert backend is not None


# ---------------------------------------------------------------------------
# lookup()
# ---------------------------------------------------------------------------


class TestLookup:
    def test_lookup_miss_returns_none(self) -> None:
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 1024)
        assert result is None

    def test_lookup_hit_returns_bytes(self) -> None:
        blob = _encode_vec([1.0, 2.0, 3.0])
        pool = _make_mock_pool(fetchone_return=(blob,))
        backend = _make_backend(pool)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 3)
        assert result == blob

    def test_lookup_hit_wraps_memoryview_to_bytes(self) -> None:
        """If PG driver returns memoryview/bytearray, backend converts to bytes."""
        blob = _encode_vec([1.0])
        pool = _make_mock_pool(fetchone_return=(memoryview(blob),))
        backend = _make_backend(pool)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 1)
        assert isinstance(result, bytes)
        assert result == blob

    def test_lookup_calls_select_with_correct_params(self) -> None:
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        backend.lookup("mykey", "cohere", "embed-v4.0", 1536)
        sql_fragment = str(conn.execute.call_args)
        assert "mykey" in sql_fragment
        assert "cohere" in sql_fragment
        assert "embed-v4.0" in sql_fragment
        assert "1536" in sql_fragment

    def test_lookup_fail_open_returns_none(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        # Schema creation also fails — that's OK per fail-open
        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 1024)
        assert result is None


# ---------------------------------------------------------------------------
# upsert()
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_upsert_calls_insert_on_conflict_do_update(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        blob = _encode_vec([1.0, 2.0])
        now = time.time()
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 2, blob, now, now)
        sql_arg = str(conn.execute.call_args)
        assert "INSERT INTO query_embedding_cache" in sql_arg
        assert "ON CONFLICT" in sql_arg
        assert "DO UPDATE" in sql_arg

    def test_upsert_commits(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        blob = _encode_vec([1.0])
        now = time.time()
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 1, blob, now, now)
        conn.commit.assert_called()

    def test_upsert_fail_open(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        # Must not raise
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 1, b"\x00", 0.0, 0.0)


# ---------------------------------------------------------------------------
# touch_last_used()
# ---------------------------------------------------------------------------


class TestTouchLastUsed:
    def test_touch_last_used_calls_update(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        backend.touch_last_used("k1", "voyage-ai", "voyage-code-3", 1024, time.time())
        sql_arg = str(conn.execute.call_args)
        assert "UPDATE query_embedding_cache" in sql_arg
        assert "last_used" in sql_arg

    def test_touch_last_used_commits(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        backend.touch_last_used("k1", "voyage-ai", "voyage-code-3", 1024, time.time())
        conn.commit.assert_called()

    def test_touch_last_used_fail_open(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        # Must not raise
        backend.touch_last_used("k1", "voyage-ai", "voyage-code-3", 1024, 0.0)


# ---------------------------------------------------------------------------
# prune_to_max()
# ---------------------------------------------------------------------------


class TestPruneToMax:
    def test_prune_to_max_returns_zero_when_under_cap(self) -> None:
        # total=3, cap=10 -> nothing to delete
        pool = _make_mock_pool(fetchone_return=(3,), rowcount=0)
        backend = _make_backend(pool)
        deleted = backend.prune_to_max(10)
        assert deleted == 0

    def test_prune_to_max_deletes_excess(self) -> None:
        # total=5, cap=3 -> delete 2
        pool = _make_mock_pool(fetchone_return=(5,), rowcount=2)
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        deleted = backend.prune_to_max(3)
        # Should have called DELETE
        all_calls = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "DELETE" in all_calls
        assert deleted == 2

    def test_prune_to_max_fail_open_returns_zero(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        result = backend.prune_to_max(100)
        assert result == 0


class TestPruneToMaxPurePrimitive:
    """Verify prune_to_max is a PURE primitive with no floor inside.

    The >=100 safe floor must NOT be enforced inside the backend — it lives
    exclusively at the config-resolution layer (QueryEmbeddingCache._resolve_max_entries).
    These tests verify the SQL is called with the EXACT cap passed by the caller,
    even when that cap is smaller than 100.
    """

    def test_small_cap_passed_directly_as_offset(self) -> None:
        """prune_to_max(3) must use OFFSET 3 in the DELETE, NOT OFFSET 100."""
        pool = _make_mock_pool(rowcount=2)
        backend = _make_backend(pool)
        conn = _get_conn(pool)

        backend.prune_to_max(3)

        # The SQL must pass the exact cap (3) as the OFFSET parameter
        all_calls = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "DELETE" in all_calls
        # Verify OFFSET 3 is in the call args (not 100)
        delete_call = next(c for c in conn.execute.call_args_list if "DELETE" in str(c))
        args = list(delete_call.args) if delete_call.args else []
        kwargs = delete_call.kwargs if delete_call.kwargs else {}
        # The parameter tuple should contain 3 (the cap), not 100
        params = args[1] if len(args) > 1 else kwargs.get("params", ())
        assert 3 in params, (
            f"Expected OFFSET 3 in DELETE params, got: {params}. "
            "The primitive must be pure — no floor inside."
        )

    def test_zero_cap_passed_directly_as_offset(self) -> None:
        """prune_to_max(0) must use OFFSET 0, not OFFSET 100."""
        pool = _make_mock_pool(rowcount=5)
        backend = _make_backend(pool)
        conn = _get_conn(pool)

        backend.prune_to_max(0)

        delete_call = next(c for c in conn.execute.call_args_list if "DELETE" in str(c))
        args = list(delete_call.args) if delete_call.args else []
        kwargs = delete_call.kwargs if delete_call.kwargs else {}
        params = args[1] if len(args) > 1 else kwargs.get("params", ())
        assert 0 in params, (
            f"Expected OFFSET 0 in DELETE params, got: {params}. "
            "The primitive must be pure — no floor inside."
        )

    def test_no_min_cap_constant_in_module(self) -> None:
        """The backend module must NOT contain _MIN_CAP floor logic."""
        import inspect
        from code_indexer.server.storage.postgres import query_embedding_cache_backend

        source = inspect.getsource(query_embedding_cache_backend)
        assert "_MIN_CAP" not in source, (
            "_MIN_CAP found in postgres backend — floor must live at "
            "QueryEmbeddingCache._resolve_max_entries, NOT in the primitive."
        )


# ---------------------------------------------------------------------------
# total_entries()
# ---------------------------------------------------------------------------


class TestTotalEntries:
    def test_total_entries_returns_count(self) -> None:
        pool = _make_mock_pool(fetchone_return=(42,))
        backend = _make_backend(pool)
        assert backend.total_entries() == 42

    def test_total_entries_returns_zero_on_none_row(self) -> None:
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)
        assert backend.total_entries() == 0

    def test_total_entries_fail_open_returns_zero(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        assert backend.total_entries() == 0


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_executes_delete(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        backend.clear()
        sql_arg = str(conn.execute.call_args)
        assert "DELETE FROM query_embedding_cache" in sql_arg

    def test_clear_commits(self) -> None:
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        conn = _get_conn(pool)
        backend.clear()
        conn.commit.assert_called()

    def test_clear_fail_open(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("DB down")
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        backend = QueryEmbeddingCachePostgresBackend.__new__(
            QueryEmbeddingCachePostgresBackend
        )
        backend._pool = pool
        # Must not raise
        backend.clear()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_pg_backend_satisfies_protocol(self) -> None:
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )
        from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend

        pool = _make_mock_pool()
        backend = QueryEmbeddingCachePostgresBackend(pool)
        assert isinstance(backend, QueryEmbeddingCacheBackend)
