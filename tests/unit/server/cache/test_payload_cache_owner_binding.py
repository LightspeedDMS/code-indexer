"""PayloadCache: cached content is served only to its creator.

Every handle-producing store records the creating user with the entry, in
the same write; the owner-checked reads (used by the generic retrieval
doors) answer another user, an empty owner and an entry with no recorded
creator exactly like a missing handle. Key-addressed entries have no
creator. Tables created before the owner column existed gain it in place.

Real stores over every storage path (see ``_owner_bound_stores``).
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest

from code_indexer.server.cache.payload_cache import (
    CacheNotFoundError,
    PayloadCache,
    PayloadCacheConfig,
)
from tests.unit.server.cache._owner_bound_stores import (  # noqa: F401 (fixture)
    ALICE,
    BOB,
    LARGE_CONTENT,
    CacheStore,
    cache_store_fixture,
    drop_postgres_schema,
    new_postgres_backend,
    pg_dsn,
    postgres_schema_or_skip,
)

MIGRATION_075 = (
    Path(__file__).resolve().parents[4]
    / "src/code_indexer/server/storage/postgres/migrations/sql"
    / "075_payload_cache_owner.sql"
)
MISSING = "00000000-0000-4000-8000-000000000000"


def _not_found_message(cache: PayloadCache, handle: str) -> str:
    with pytest.raises(CacheNotFoundError) as exc_info:
        cache.retrieve_owned(handle, BOB)
    return str(exc_info.value)


class TestHandleProducingStoresRecordTheCreator:
    def test_store_batch_records_owner_on_every_row(
        self, cache_store: CacheStore
    ) -> None:
        handles = cache_store.cache.store_batch(["a" * 300, "b" * 300], owner=ALICE)

        assert [cache_store.owner_of(h) for h in handles] == [ALICE, ALICE]

    def test_store_records_owner(self, cache_store: CacheStore) -> None:
        handle = cache_store.cache.store(LARGE_CONTENT, owner=ALICE)

        assert cache_store.owner_of(handle) == ALICE

    def test_store_batch_with_keys_records_owner(self, cache_store: CacheStore) -> None:
        cache_store.cache.store_batch_with_keys(
            [("page-1", "p1"), ("manifest-1", "m1")], owner=ALICE
        )

        assert cache_store.owner_of("page-1") == ALICE
        assert cache_store.owner_of("manifest-1") == ALICE

    def test_truncate_result_records_owner(self, cache_store: CacheStore) -> None:
        result = cache_store.cache.truncate_result(LARGE_CONTENT, owner=ALICE)

        assert cache_store.owner_of(result["cache_handle"]) == ALICE

    def test_key_addressed_entry_has_no_creator(self, cache_store: CacheStore) -> None:
        cache_store.cache.store_with_key("discovery:job-1", LARGE_CONTENT)

        assert cache_store.owner_of("discovery:job-1") is None


class TestOwnerCheckedReads:
    def test_creator_reads_pages_and_full_content(
        self, cache_store: CacheStore
    ) -> None:
        [handle] = cache_store.cache.store_batch([LARGE_CONTENT], owner=ALICE)

        page = cache_store.cache.retrieve_owned(handle, ALICE, page=1)
        full = cache_store.cache.retrieve_full_owned(handle, ALICE)

        assert (page.content, page.page, page.total_pages) == ("x" * 200, 1, 3)
        assert full == LARGE_CONTENT

    def test_other_user_gets_the_missing_handle_error(
        self, cache_store: CacheStore
    ) -> None:
        [handle] = cache_store.cache.store_batch([LARGE_CONTENT], owner=ALICE)

        with pytest.raises(CacheNotFoundError) as paged:
            cache_store.cache.retrieve_owned(handle, BOB)
        with pytest.raises(CacheNotFoundError) as full:
            cache_store.cache.retrieve_full_owned(handle, BOB)

        expected = f"Cache handle not found: {handle}"
        assert str(paged.value) == str(full.value) == expected
        assert _not_found_message(cache_store.cache, MISSING) == (
            f"Cache handle not found: {MISSING}"
        )

    def test_empty_owner_is_never_served(self, cache_store: CacheStore) -> None:
        handle = cache_store.cache.store(LARGE_CONTENT)

        with pytest.raises(CacheNotFoundError):
            cache_store.cache.retrieve_owned(handle, "")

    @pytest.mark.parametrize("producer", ["store", "store_with_key"])
    def test_entry_without_creator_is_not_served_to_anyone(
        self, cache_store: CacheStore, producer: str
    ) -> None:
        if producer == "store":
            handle = cache_store.cache.store(LARGE_CONTENT)
        else:
            handle = "temporal_query:job-9"
            cache_store.cache.store_with_key(handle, LARGE_CONTENT)

        for user in (ALICE, BOB):
            with pytest.raises(CacheNotFoundError):
                cache_store.cache.retrieve_owned(handle, user)
            with pytest.raises(CacheNotFoundError):
                cache_store.cache.retrieve_full_owned(handle, user)
        # The route that wrote a key-addressed entry still reads it.
        assert cache_store.cache.retrieve_full(handle) == LARGE_CONTENT


def _old_facade_table(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            "CREATE TABLE payload_cache (handle TEXT PRIMARY KEY, content TEXT "
            "NOT NULL, created_at REAL NOT NULL, total_size INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO payload_cache VALUES ('old-handle', 'old', 9e12, 3)")
    conn.close()


def _old_backend_table_sql() -> Tuple[str, str]:
    return (
        "CREATE TABLE payload_cache (cache_handle TEXT PRIMARY KEY, content TEXT "
        "NOT NULL, preview TEXT NOT NULL, created_at TEXT NOT NULL, ttl_seconds "
        "INTEGER NOT NULL, node_id TEXT)",
        "INSERT INTO payload_cache VALUES ('old-handle', 'old', 'old', "
        "'2999-01-01T00:00:00+00:00', 900, NULL)",
    )


def _columns_sqlite(db_path: Path) -> List[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(payload_cache)")]
    finally:
        conn.close()


class TestTablesThatPredateTheOwnerColumn:
    def test_facade_table_gains_column_and_old_rows_are_not_served(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "old_facade.db"
        _old_facade_table(db_path)
        cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())

        cache.initialize()

        assert "owner" in _columns_sqlite(db_path)
        with pytest.raises(CacheNotFoundError):
            cache.retrieve_owned("old-handle", ALICE)
        assert cache.retrieve_full("old-handle") == "old"

    def test_sqlite_backend_table_gains_column_and_old_rows_are_not_served(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
            PayloadCacheSqliteBackend,
        )

        db_path = tmp_path / "old_backend.db"
        conn = sqlite3.connect(str(db_path))
        with conn:
            for sql in _old_backend_table_sql():
                conn.execute(sql)
        conn.close()
        cache = PayloadCache(
            db_path=db_path,
            config=PayloadCacheConfig(),
            storage_backend=PayloadCacheSqliteBackend(str(db_path)),
        )

        assert "owner" in _columns_sqlite(db_path)
        with pytest.raises(CacheNotFoundError):
            cache.retrieve_owned("old-handle", ALICE)
        assert cache.retrieve_full("old-handle") == "old"

    def test_postgres_backend_table_gains_column_and_old_rows_are_not_served(
        self,
    ) -> None:
        import psycopg

        dsn, schema = postgres_schema_or_skip()
        pool: Any = None
        try:
            with psycopg.connect(pg_dsn(dsn, schema), autocommit=True) as conn:
                for sql in _old_backend_table_sql():
                    conn.execute(sql)
            backend, pool = new_postgres_backend(dsn, schema)
            cache = PayloadCache(
                db_path=Path("unused.db"),
                config=PayloadCacheConfig(),
                storage_backend=backend,
            )

            with pytest.raises(CacheNotFoundError):
                cache.retrieve_owned("old-handle", ALICE)
            assert cache.retrieve_full("old-handle") == "old"
            [handle] = cache.store_batch([LARGE_CONTENT], owner=ALICE)
            assert cache.retrieve_full_owned(handle, ALICE) == LARGE_CONTENT
        finally:
            if pool is not None:
                pool.close()
            drop_postgres_schema(dsn, schema)


class TestPostgresMigration075:
    def _columns(self, dsn: str, schema: str) -> List[str]:
        import psycopg

        with psycopg.connect(pg_dsn(dsn, schema), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'payload_cache'",
                (schema,),
            ).fetchall()
        return [r[0] for r in rows]

    def test_adds_owner_to_existing_table_and_is_idempotent(self) -> None:
        import psycopg

        dsn, schema = postgres_schema_or_skip()
        try:
            with psycopg.connect(pg_dsn(dsn, schema), autocommit=True) as conn:
                conn.execute(_old_backend_table_sql()[0])
                conn.execute(MIGRATION_075.read_text())
                conn.execute(MIGRATION_075.read_text())
            assert "owner" in self._columns(dsn, schema)
        finally:
            drop_postgres_schema(dsn, schema)

    def test_is_a_no_op_when_the_table_does_not_exist_yet(self) -> None:
        import psycopg

        dsn, schema = postgres_schema_or_skip()
        try:
            with psycopg.connect(pg_dsn(dsn, schema), autocommit=True) as conn:
                conn.execute(MIGRATION_075.read_text())
            assert self._columns(dsn, schema) == []
        finally:
            drop_postgres_schema(dsn, schema)


class TestPostgresBatchStaysOneTransaction:
    """Driver-faithful fake: psycopg v3 has executemany on the cursor only."""

    def test_store_batch_writes_owner_in_every_row_with_one_commit(self) -> None:
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        executemany_calls: List[Tuple[str, List[Any]]] = []
        commits: List[int] = []

        class FakeCursor:
            def executemany(self, sql: str, rows: List[Any]) -> None:
                executemany_calls.append((sql, list(rows)))

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

        class FakeConn:
            def execute(self, sql: str, params: Any = None) -> None:
                return None

            def cursor(self) -> FakeCursor:
                return FakeCursor()

            def commit(self) -> None:
                commits.append(1)

            def __enter__(self) -> "FakeConn":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

        class FakePool:
            def connection(self) -> FakeConn:
                return FakeConn()

        backend = PayloadCachePostgresBackend.__new__(PayloadCachePostgresBackend)
        backend._pool = FakePool()  # type: ignore[assignment]
        cache = PayloadCache(
            db_path=Path("unused.db"),
            config=PayloadCacheConfig(),
            storage_backend=backend,
        )

        handles = cache.store_batch(["a" * 10, "b" * 10, "c" * 10], owner=ALICE)

        assert len(commits) == 1
        assert len(executemany_calls) == 1
        sql, rows = executemany_calls[0]
        assert "owner" in sql
        assert [row[0] for row in rows] == handles
        assert [row[-1] for row in rows] == [ALICE, ALICE, ALICE]


# Invalid UTF-8 stored as TEXT: any SELECT that returns this content column
# raises sqlite3.OperationalError, so an owned read that loaded it would fail
# differently from a missing handle.
_UNREADABLE = "CAST(x'ff' AS TEXT)"


def _insert_unreadable_sqlite_rows(kind: str, db_path: Path) -> None:
    """A row owned by BOB and an owner-less row, both with unreadable content."""
    if kind == "facade":
        sql = (
            "INSERT INTO payload_cache (handle, content, created_at, total_size, "
            f"owner) VALUES (?, {_UNREADABLE}, 9e12, 1, ?)"
        )
    else:
        sql = (
            "INSERT INTO payload_cache (cache_handle, content, preview, created_at, "
            f"ttl_seconds, node_id, owner) VALUES (?, {_UNREADABLE}, '', "
            "'2999-01-01T00:00:00+00:00', 900, NULL, ?)"
        )
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(sql, ("foreign-handle", BOB))
        conn.execute(sql, ("ownerless-handle", None))
    conn.close()


@pytest.mark.parametrize("kind", ["facade", "sqlite"])
class TestOwnedReadsNeverLoadForeignContentSqlite:
    def _cache(self, kind: str, tmp_path: Path) -> PayloadCache:
        from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
            PayloadCacheSqliteBackend,
        )

        db_path = tmp_path / "unreadable.db"
        backend = PayloadCacheSqliteBackend(str(db_path)) if kind == "sqlite" else None
        cache = PayloadCache(
            db_path=db_path, config=PayloadCacheConfig(), storage_backend=backend
        )
        cache.initialize()
        _insert_unreadable_sqlite_rows(kind, db_path)
        return cache

    @pytest.mark.parametrize("handle", ["foreign-handle", "ownerless-handle"])
    def test_owned_reads_do_not_load_content_of_rows_the_caller_did_not_create(
        self, kind: str, tmp_path: Path, handle: str
    ) -> None:
        cache = self._cache(kind, tmp_path)
        # Premise: reading this content column at all raises.
        with pytest.raises(sqlite3.OperationalError):
            cache.retrieve_full(handle)

        with pytest.raises(CacheNotFoundError):
            cache.retrieve_owned(handle, ALICE)
        with pytest.raises(CacheNotFoundError):
            cache.retrieve_full_owned(handle, ALICE)


class _TracingConnection:
    """Proxy over a real psycopg connection recording (sql, fetched row)."""

    def __init__(self, conn: Any, log: List[Tuple[str, Any]]) -> None:
        self._conn = conn
        self._log = log

    def execute(self, sql: str, params: Any = None) -> Any:
        return _TracingCursor(self._conn.execute(sql, params), sql, self._log)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _TracingCursor:
    def __init__(self, cursor: Any, sql: str, log: List[Tuple[str, Any]]) -> None:
        self._cursor = cursor
        self._sql = sql
        self._log = log

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        self._log.append((" ".join(self._sql.split()), row))
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _TracingPool:
    def __init__(self, pool: Any, log: List[Tuple[str, Any]]) -> None:
        self._pool = pool
        self._log = log

    @contextlib.contextmanager
    def connection(self) -> Iterator[_TracingConnection]:
        with self._pool.connection() as conn:
            yield _TracingConnection(conn, self._log)


class TestOwnedReadsArePostgresOwnerFiltered:
    @pytest.mark.parametrize("handle", ["foreign-handle", "ownerless-handle", MISSING])
    def test_owned_read_is_one_owner_filtered_query_returning_no_row(
        self, handle: str
    ) -> None:
        dsn, schema = postgres_schema_or_skip()
        pool: Any = None
        try:
            backend, pool = new_postgres_backend(dsn, schema)
            cache = PayloadCache(
                db_path=Path("unused.db"),
                config=PayloadCacheConfig(),
                storage_backend=backend,
            )
            cache.store_with_key("ownerless-handle", LARGE_CONTENT)
            cache.store_batch_with_keys([("foreign-handle", LARGE_CONTENT)], owner=BOB)
            [own] = cache.store_batch([LARGE_CONTENT], owner=ALICE)
            log: List[Tuple[str, Any]] = []
            backend._pool = _TracingPool(pool, log)

            for read in (cache.retrieve_owned, cache.retrieve_full_owned):
                log.clear()
                with pytest.raises(CacheNotFoundError):
                    read(handle, ALICE)
                assert len(log) == 1, log
                sql, row = log[0]
                assert "owner = %s" in sql
                assert row is None

            log.clear()
            assert cache.retrieve_full_owned(own, ALICE) == LARGE_CONTENT
            assert len(log) == 1 and "owner = %s" in log[0][0]
        finally:
            if pool is not None:
                pool.close()
            drop_postgres_schema(dsn, schema)
