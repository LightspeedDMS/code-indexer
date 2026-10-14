"""Shared fixture for the cached-content owner-binding tests (imported,
never collected: no ``test_`` prefix).

`cache_store` is parametrized over every PayloadCache storage path:
  - ``facade``: PayloadCache with no backend (its own SQLite schema);
  - ``sqlite``: PayloadCacheSqliteBackend (solo server);
  - ``postgres``: PayloadCachePostgresBackend on TEST_POSTGRES_DSN, inside a
    per-test schema that is dropped afterwards (skips when the DSN is unset).

All stores are real; nothing is mocked.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig

logger = logging.getLogger(__name__)

ALICE = "alice"
BOB = "bob"
PREVIEW_SIZE = 100
LARGE_CONTENT = "x" * 500


class CacheStore:
    """One real PayloadCache plus a raw SQL observer over its table."""

    def __init__(
        self, kind: str, cache: PayloadCache, location: str, schema: str
    ) -> None:
        self.kind = kind
        self.cache = cache
        self.location = location
        self.schema = schema
        self.pools: List[Any] = []

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Any]:
        """Run one committed statement on an independent connection."""
        if self.kind != "postgres":
            conn = sqlite3.connect(self.location)
            try:
                with conn:
                    cur = conn.execute(sql.replace("%s", "?"), params)
                    return list(cur.fetchall()) if cur.description else []
            finally:
                conn.close()
        import psycopg

        pg = psycopg.connect(pg_dsn(self.location, self.schema), autocommit=True)
        try:
            pg_cur = pg.execute(sql, params)
            return list(pg_cur.fetchall()) if pg_cur.description else []
        finally:
            pg.close()

    @property
    def handle_column(self) -> str:
        return "handle" if self.kind == "facade" else "cache_handle"

    def owner_of(self, handle: str) -> Optional[str]:
        rows = self.execute(
            f"SELECT owner FROM payload_cache WHERE {self.handle_column} = %s",
            (handle,),
        )
        assert rows, f"no row stored for {handle}"
        return rows[0][0]  # type: ignore[no-any-return]

    def close(self) -> None:
        self.cache.close()
        for pool in self.pools:
            try:
                pool.close()
            except Exception:  # noqa: BLE001 -- teardown must not mask results
                logger.warning("pool close failed", exc_info=True)


def pg_dsn(dsn: str, schema: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, options=f"-csearch_path={schema}")


def _config() -> PayloadCacheConfig:
    return PayloadCacheConfig(preview_size_chars=PREVIEW_SIZE, max_fetch_size_chars=200)


def _sqlite_store(kind: str, tmp_path: Path) -> CacheStore:
    db_path = tmp_path / "payload_cache.db"
    backend: Any = None
    if kind == "sqlite":
        from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
            PayloadCacheSqliteBackend,
        )

        backend = PayloadCacheSqliteBackend(str(db_path))
    cache = PayloadCache(db_path=db_path, config=_config(), storage_backend=backend)
    cache.initialize()
    return CacheStore(kind, cache, str(db_path), "")


def postgres_schema_or_skip() -> Tuple[str, str]:
    """(dsn, fresh schema name) or pytest.skip when PostgreSQL is absent."""
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not available")
    schema = f"payload_owner_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
    except Exception as exc:  # noqa: BLE001 -- unreachable server means skip
        pytest.skip(f"Cannot use PostgreSQL: {exc}")
    return dsn, schema


def drop_postgres_schema(dsn: str, schema: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def new_postgres_backend(dsn: str, schema: str) -> Tuple[Any, Any]:
    """(backend, pool) over the given schema."""
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )

    pool = ConnectionPool(pg_dsn(dsn, schema), min_size=1, max_size=2)
    return PayloadCachePostgresBackend(pool), pool


@pytest.fixture(name="cache_store", params=["facade", "sqlite", "postgres"])
def cache_store_fixture(request: Any, tmp_path: Path) -> Iterator[CacheStore]:
    if request.param != "postgres":
        sqlite_store = _sqlite_store(request.param, tmp_path)
        try:
            yield sqlite_store
        finally:
            sqlite_store.close()
        return

    dsn, schema = postgres_schema_or_skip()
    store: Optional[CacheStore] = None
    pool: Any = None
    try:
        backend, pool = new_postgres_backend(dsn, schema)
        cache = PayloadCache(
            db_path=Path("unused_payload_cache.db"),
            config=_config(),
            storage_backend=backend,
        )
        cache.initialize()
        store = CacheStore("postgres", cache, dsn, schema)
        store.pools.append(pool)
        pool = None
        yield store
    finally:
        if store is not None:
            store.close()
        if pool is not None:
            pool.close()
        drop_postgres_schema(dsn, schema)
