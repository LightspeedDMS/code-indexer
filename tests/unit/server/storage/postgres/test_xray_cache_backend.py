"""Tests for XrayCachePostgresBackend — Epic #1019 / cluster xray evaluator cache.

Uses a MagicMock connection pool (no real PostgreSQL required).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_mock_pool(fetchone_return=None, rowcount: int = 0) -> MagicMock:
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
    # Returning Any: chained MagicMock attribute accesses (.return_value.__enter__
    # .return_value) produce Any under mypy — no more precise type is expressible.
    return pool.connection.return_value.__enter__.return_value


@pytest.fixture()
def pool_and_backend():
    """Backend configured for a cache miss (fetchone returns None)."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(fetchone_return=None)
    backend = XrayCachePostgresBackend(pool)
    _get_conn(pool).reset_mock()  # clear schema-setup calls
    return pool, backend


@pytest.fixture()
def store_backend():
    """Backend and connection mock reset for testing store() and cleanup."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(rowcount=0)
    backend = XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)
    conn.reset_mock()
    return backend, conn


@pytest.fixture()
def pool_and_fresh_backend():
    """Backend configured for a cache hit with fake .so bytes."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    fake_bytes = b"\x7fELF fake so content"
    pool = _make_mock_pool(fetchone_return=(fake_bytes,))
    backend = XrayCachePostgresBackend(pool)
    return pool, backend, fake_bytes


# ---------------------------------------------------------------------------
# Tests: schema, miss, hit
# ---------------------------------------------------------------------------


def test_schema_created_on_init() -> None:
    """XrayCachePostgresBackend.__init__ must call CREATE TABLE IF NOT EXISTS."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)

    assert conn.execute.called
    assert conn.commit.called
    combined = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "xray_evaluator_cache" in combined
    assert "IF NOT EXISTS" in combined or "if not exists" in combined.lower()


def test_fetch_returns_none_on_miss(pool_and_backend) -> None:
    """fetch() must return None when the query returns no row."""
    _, backend = pool_and_backend
    assert backend.fetch("abc123", "rustc 1.91.0") is None


def test_fetch_returns_bytes_on_fresh_hit(pool_and_fresh_backend) -> None:
    """fetch() must return the so_bytes from the row on a fresh hit."""
    _, backend, fake_bytes = pool_and_fresh_backend
    result = backend.fetch("abc123", "rustc 1.91.0")
    assert result == fake_bytes


def test_fetch_sql_filters_by_compiled_at_and_rustc_version(pool_and_backend) -> None:
    """fetch() SQL must filter by compiled_at AND rustc_version in WHERE clause."""
    pool, backend = pool_and_backend

    backend.fetch("deadbeef", "rustc 1.88.0")

    conn = _get_conn(pool)
    fetch_call = conn.execute.call_args
    sql = str(fetch_call[0][0])
    params = fetch_call[0][1]

    assert "compiled_at" in sql
    assert "rustc_version" in sql
    assert "rustc 1.88.0" in params
    assert len(params) == 3, (
        "fetch() must pass exactly 3 params (hash, version, cutoff)"
    )


def test_fetch_returns_none_on_rustc_version_mismatch(pool_and_backend) -> None:
    """fetch() returns None when rustc_version does not match (SQL returns no row)."""
    pool, backend = pool_and_backend

    result = backend.fetch("abc123", "rustc 1.70.0-mismatched")

    assert result is None
    conn = _get_conn(pool)
    params = conn.execute.call_args[0][1]
    assert "rustc 1.70.0-mismatched" in params, (
        "The mismatched rustc_version must be passed as a query param"
    )


def test_fetch_returns_none_on_stale_entry(pool_and_backend) -> None:
    """fetch() returns None when the cached entry is older than TTL.

    The compiled_at > cutoff filter in the SQL excludes stale rows at the
    database level. Verifies both the None result and that a datetime cutoff
    is the 3rd query parameter.
    """
    from datetime import datetime

    pool, backend = pool_and_backend

    result = backend.fetch("stale_hash", "rustc 1.91.0")

    assert result is None
    conn = _get_conn(pool)
    fetch_call = conn.execute.call_args
    sql = str(fetch_call[0][0])
    params = fetch_call[0][1]

    assert "compiled_at" in sql, "SQL must include compiled_at filter"
    assert len(params) == 3
    cutoff = params[2]
    assert isinstance(cutoff, datetime), (
        f"3rd param must be a datetime cutoff, got {type(cutoff)}"
    )


def test_store_issues_upsert_and_commits(store_backend) -> None:
    """store() must execute INSERT ... ON CONFLICT DO UPDATE and commit."""
    backend, conn = store_backend

    backend.store("cafebabe", "rustc 1.91.0", b"\x7fELF store test", compile_ms=350)

    assert conn.execute.called
    assert conn.commit.called
    all_sql = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "INSERT" in all_sql.upper()
    assert "ON CONFLICT" in all_sql.upper()


def test_store_triggers_cleanup_delete_after_upsert(store_backend) -> None:
    """store() must execute DELETE AFTER the UPSERT (verified by call order)."""
    backend, conn = store_backend

    backend.store("hash99", "rustc 1.91.0", b"so_data", compile_ms=100)

    sqls = [str(c) for c in conn.execute.call_args_list]
    assert len(sqls) >= 2, (
        f"store() must execute at least 2 statements, got {len(sqls)}"
    )
    insert_pos = next((i for i, s in enumerate(sqls) if "INSERT" in s.upper()), None)
    delete_pos = next((i for i, s in enumerate(sqls) if "DELETE" in s.upper()), None)
    assert insert_pos is not None, "store() must execute INSERT"
    assert delete_pos is not None, "store() must execute DELETE cleanup"
    assert insert_pos < delete_pos, "DELETE must come AFTER INSERT"


def test_cleanup_expired_issues_delete_and_returns_count() -> None:
    """_cleanup_expired() must DELETE expired rows and return the count deleted.

    Uses rowcount=3 pool (distinct from store_backend's rowcount=0).
    """
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(rowcount=3)
    backend = XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)
    conn.reset_mock()

    deleted = backend._cleanup_expired()

    assert conn.execute.called
    assert conn.commit.called
    assert deleted == 3
    all_sql = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "DELETE" in all_sql.upper()
    assert "xray_evaluator_cache" in all_sql


def test_fetch_swallows_pg_exception_returns_none() -> None:
    """fetch() must catch all exceptions and return None (never raise)."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    conn = _get_conn(pool)
    conn.execute.side_effect = RuntimeError("PG connection lost")
    backend = XrayCachePostgresBackend.__new__(XrayCachePostgresBackend)
    backend._pool = pool

    result = backend.fetch("any", "any")
    assert result is None


def test_store_swallows_pg_exception_does_not_raise() -> None:
    """store() must catch all exceptions and return without raising."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    conn = _get_conn(pool)
    conn.execute.side_effect = RuntimeError("PG write failed")
    backend = XrayCachePostgresBackend.__new__(XrayCachePostgresBackend)
    backend._pool = pool

    backend.store("any", "any", b"bytes", compile_ms=0)  # must not raise


def test_ttl_constant_is_300() -> None:
    """XRAY_CACHE_TTL_SECONDS must be 300 (5 minutes)."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XRAY_CACHE_TTL_SECONDS,
    )

    assert XRAY_CACHE_TTL_SECONDS == 300
