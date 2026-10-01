"""Unit tests for PayloadCache.retrieve_full() (Bug #1928 rework).

`retrieve_full()` returns the COMPLETE stored content for a handle,
bypassing the facade's char-window pagination math entirely -- so a
caller that stores its OWN pre-chunked pages (see
server/mcp/handlers/xray_truncation.py) can read a page back exactly as
stored, regardless of what `payload_max_fetch_size_chars` is configured
to at FETCH time (which may differ from what it was at STORE time -- Bug
#1928 P1/P2: the old design's char-window pagination baked in the
store-time width via padding, so a later config change corrupted
already-stored pages).

Both backends already fetch the FULL row before `retrieve()` slices it
(see PayloadCache.retrieve()'s own implementation for both the SQLite and
`_backend` branches) -- `retrieve_full()` has IDENTICAL memory/IO cost to
`retrieve()`, it just skips the slicing step.

Live PostgreSQL companion tests gated by TEST_POSTGRES_DSN (same
convention as test_per_consumer_rate_limiter_live_pg_1332.py) -- skipped
when no real PostgreSQL is reachable.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest

from code_indexer.server.cache.payload_cache import (
    CacheNotFoundError,
    PayloadCache,
    PayloadCacheConfig,
)


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "payload_cache.db"


@pytest.fixture
def cache(temp_db_path: Path) -> Generator[PayloadCache, None, None]:
    config = PayloadCacheConfig(
        preview_size_chars=200,
        max_fetch_size_chars=200,
        cache_ttl_seconds=900,
        cleanup_interval_seconds=60,
    )
    c = PayloadCache(db_path=temp_db_path, config=config)
    c.initialize()
    yield c
    c.close()


class TestRetrieveFullSqliteBasics:
    def test_returns_full_content_larger_than_one_page(
        self, cache: PayloadCache
    ) -> None:
        """The whole point: content far larger than max_fetch_size_chars
        (200 here) comes back WHOLE from retrieve_full(), never windowed."""
        content = "x" * 5000
        handle = cache.store(content)

        full = cache.retrieve_full(handle)

        assert full == content
        assert len(full) == 5000

    def test_raises_cache_not_found_for_unknown_handle(
        self, cache: PayloadCache
    ) -> None:
        with pytest.raises(CacheNotFoundError):
            cache.retrieve_full("does-not-exist")

    def test_small_content_round_trips_exactly(self, cache: PayloadCache) -> None:
        handle = cache.store("small")
        assert cache.retrieve_full(handle) == "small"


class TestRetrieveFullSqliteConfigChange:
    def test_unaffected_by_max_fetch_size_chars_changed_after_store(
        self, temp_db_path: Path
    ) -> None:
        """Storing under one config, then reading with a DIFFERENT
        max_fetch_size_chars (simulating a Web UI config change or a
        restart with a new value), must still return the exact original
        content -- retrieve_full() never consults max_fetch_size_chars."""
        store_config = PayloadCacheConfig(
            preview_size_chars=100, max_fetch_size_chars=100
        )
        store_cache = PayloadCache(db_path=temp_db_path, config=store_config)
        try:
            store_cache.initialize()
            content = "y" * 3333
            handle = store_cache.store(content)
        finally:
            store_cache.close()

        fetch_config = PayloadCacheConfig(
            preview_size_chars=9999, max_fetch_size_chars=9999
        )
        fetch_cache = PayloadCache(db_path=temp_db_path, config=fetch_config)
        try:
            fetch_cache.initialize()
            full = fetch_cache.retrieve_full(handle)
        finally:
            fetch_cache.close()

        assert full == content


# ---------------------------------------------------------------------------
# Live PostgreSQL companion (gated by TEST_POSTGRES_DSN)
# ---------------------------------------------------------------------------

HAS_PSYCOPG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn() -> Generator[str, None, None]:
    if not HAS_PSYCOPG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    yield dsn


def _make_pg_cache(
    pg_dsn: str, max_fetch_size_chars: int
) -> "tuple[PayloadCache, Any]":
    """Build a fresh ConnectionPool + PayloadCachePostgresBackend + facade
    pointed at the SAME `payload_cache` table. try/finally so a mid-setup
    failure (e.g. schema creation) can never leak the pool."""
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )

    pool = ConnectionPool(pg_dsn, min_size=1, max_size=2)
    try:
        backend = PayloadCachePostgresBackend(pool)
        config = PayloadCacheConfig(
            preview_size_chars=max_fetch_size_chars,
            max_fetch_size_chars=max_fetch_size_chars,
        )
        cache = PayloadCache(
            db_path=Path("/unused"), config=config, storage_backend=backend
        )
        cache.initialize()
    except Exception:
        pool.close()
        raise
    return cache, pool


@pytest.fixture
def pg_backend_cache(pg_dsn: str) -> Generator[PayloadCache, None, None]:
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS payload_cache")

    cache, pool = _make_pg_cache(pg_dsn, max_fetch_size_chars=100)
    try:
        yield cache
    finally:
        cache.close()
        pool.close()
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS payload_cache")


class TestRetrieveFullLivePostgresBasics:
    def test_returns_full_content_larger_than_one_page(
        self, pg_backend_cache: PayloadCache
    ) -> None:
        content = "z" * 5000
        handle = pg_backend_cache.store(content)

        full = pg_backend_cache.retrieve_full(handle)

        assert full == content

    def test_raises_cache_not_found_for_unknown_handle(
        self, pg_backend_cache: PayloadCache
    ) -> None:
        with pytest.raises(CacheNotFoundError):
            pg_backend_cache.retrieve_full("does-not-exist")


class TestRetrieveFullLivePostgresConfigChange:
    def test_unaffected_by_max_fetch_size_chars_changed_after_store(
        self, pg_dsn: str
    ) -> None:
        """Same config-change proof as the SQLite suite, but for the PG
        backend: two independent PayloadCache instances (simulating two
        cluster nodes with different live config) share ONE real
        PostgreSQL payload_cache table."""
        import psycopg

        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS payload_cache")

        store_cache, store_pool = _make_pg_cache(pg_dsn, max_fetch_size_chars=100)
        try:
            content = "w" * 3333
            handle = store_cache.store(content)
        finally:
            store_cache.close()
            store_pool.close()

        fetch_cache, fetch_pool = _make_pg_cache(pg_dsn, max_fetch_size_chars=9999)
        try:
            full = fetch_cache.retrieve_full(handle)
        finally:
            fetch_cache.close()
            fetch_pool.close()
            with psycopg.connect(pg_dsn, autocommit=True) as conn:
                conn.execute("DROP TABLE IF EXISTS payload_cache")

        assert full == content
