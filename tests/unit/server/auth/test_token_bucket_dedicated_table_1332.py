"""
Tests for PR #1332 review fix: TokenBucketManager dedicated-table support.

The PerConsumerRateLimiter admission-control gate hashes caller credentials
(SHA-256) and must NEVER share the token_bucket_state table used by the auth
login limiter -- a hashed consumer key could theoretically collide with (or
simply co-mingle rows alongside) real usernames in that table. This module
proves TokenBucketManager can be parameterized to a dedicated table/key-column
pair while its default construction remains byte-identical to the existing
auth behaviour (zero regression for the login rate limiter).

Uses real SQLite as a stand-in for PostgreSQL (same established pattern as
test_token_bucket_pg.py) -- no mocking of the manager under test.
"""

import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

import pytest

from code_indexer.server.auth.token_bucket import TokenBucketManager


def _translate_sql(sql: str) -> str:
    sql = sql.replace("%s", "?")
    sql = sql.replace("EXCLUDED.", "excluded.")
    sql = sql.replace("LEAST(", "MIN(")
    sql = sql.replace("GREATEST(", "MAX(")
    return sql


class FakeCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sqlite_cursor: Optional[sqlite3.Cursor] = None

    def execute(self, sql: str, params=None) -> "FakeCursor":
        sql = _translate_sql(sql)
        if params is not None:
            self._sqlite_cursor = self._conn.execute(sql, params)
        else:
            self._sqlite_cursor = self._conn.execute(sql)
        return self

    @property
    def rowcount(self) -> int:
        if self._sqlite_cursor is None:
            return -1
        return self._sqlite_cursor.rowcount

    def fetchone(self):
        if self._sqlite_cursor is None:
            return None
        return self._sqlite_cursor.fetchone()

    def fetchall(self):
        if self._sqlite_cursor is None:
            return []
        return self._sqlite_cursor.fetchall()

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args) -> None:
        pass


class FakeConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=None) -> FakeCursor:
        return FakeCursor(self._conn).execute(sql, params)

    def cursor(self, row_factory=None) -> FakeCursor:
        return FakeCursor(self._conn)

    def commit(self):
        self._conn.commit()


class FakePool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[FakeConnection, None, None]:
        yield FakeConnection(self._conn)


@pytest.fixture
def dual_table_conn():
    """SQLite DB with BOTH the legacy auth table and the new consumer table."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_bucket_state (
            username TEXT PRIMARY KEY,
            tokens REAL NOT NULL DEFAULT 10.0,
            last_refill REAL NOT NULL,
            last_access REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consumer_rate_limit_state (
            consumer_key TEXT PRIMARY KEY,
            tokens REAL NOT NULL DEFAULT 10.0,
            last_refill REAL NOT NULL,
            last_access REAL NOT NULL
        )
        """
    )
    conn.commit()
    yield conn
    conn.close()


class TestTableNameParameterization:
    def test_default_table_and_key_column_match_legacy_schema(self) -> None:
        manager = TokenBucketManager()
        assert manager._table_name == "token_bucket_state"
        assert manager._key_column == "username"

    def test_custom_table_name_and_key_column_used_in_sql(
        self, dual_table_conn
    ) -> None:
        pool = FakePool(dual_table_conn)
        manager = TokenBucketManager(
            table_name="consumer_rate_limit_state", key_column="consumer_key"
        )
        manager.set_connection_pool(pool)

        allowed, _ = manager.consume("deadbeef" * 4)
        assert allowed is True

        row = dual_table_conn.execute(
            "SELECT consumer_key FROM consumer_rate_limit_state"
        ).fetchone()
        assert row is not None
        assert row[0] == "deadbeef" * 4

    def test_invalid_table_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketManager(table_name="token_bucket_state; DROP TABLE users")

    def test_invalid_key_column_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketManager(key_column="username; DROP TABLE users")

    def test_trailing_newline_identifier_rejected(self) -> None:
        """Regex hardening (code review LOW note): Python's `$` anchor matches
        BEFORE a trailing newline, so a naive `^...$` pattern would let
        "token_bucket_state\n" slip through validation. Not currently
        exploitable (identifiers are constructor constants, never request
        data), but the guard must be airtight defense-in-depth."""
        with pytest.raises(ValueError):
            TokenBucketManager(table_name="token_bucket_state\n")
        with pytest.raises(ValueError):
            TokenBucketManager(key_column="username\n")


class TestDedicatedTableIsolation:
    def test_consumer_table_writes_never_touch_token_bucket_state(
        self, dual_table_conn
    ) -> None:
        """A hashed consumer key equal to a real username string must not
        create/mutate any row in token_bucket_state when using the dedicated
        consumer_rate_limit_state table."""
        pool = FakePool(dual_table_conn)
        manager = TokenBucketManager(
            table_name="consumer_rate_limit_state", key_column="consumer_key"
        )
        manager.set_connection_pool(pool)

        # Use a key string that collides with a plausible real username.
        colliding_key = "alice"
        manager.consume(colliding_key)
        manager.consume(colliding_key)

        auth_rows = dual_table_conn.execute(
            "SELECT * FROM token_bucket_state"
        ).fetchall()
        assert auth_rows == []

        consumer_rows = dual_table_conn.execute(
            "SELECT consumer_key FROM consumer_rate_limit_state"
        ).fetchall()
        assert consumer_rows == [("alice",)]

    def test_legacy_username_table_unaffected_by_default_construction(
        self, dual_table_conn
    ) -> None:
        pool = FakePool(dual_table_conn)
        manager = TokenBucketManager()  # defaults: token_bucket_state/username
        manager.set_connection_pool(pool)
        manager.consume("alice")

        consumer_rows = dual_table_conn.execute(
            "SELECT * FROM consumer_rate_limit_state"
        ).fetchall()
        assert consumer_rows == []
