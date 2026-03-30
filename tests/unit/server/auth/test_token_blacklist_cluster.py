"""Tests for TokenBlacklist cluster-aware implementation (Bug #583)."""

import sqlite3
import tempfile
import os


class TestTokenBlacklistLocal:
    """Tests for in-memory (local) token blacklist behavior."""

    def test_local_add_and_contains(self):
        """In-memory add/contains works without any DB backend."""
        from code_indexer.server.app import TokenBlacklist

        bl = TokenBlacklist()
        assert not bl.contains("jti-1")
        bl.add("jti-1")
        assert bl.contains("jti-1")
        assert not bl.contains("jti-2")

    def test_local_fast_path(self):
        """Local set returns True immediately, even when DB backend is set."""
        from code_indexer.server.app import TokenBlacklist

        # Set up SQLite backend so DB path is configured
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS token_blacklist "
                "(jti TEXT PRIMARY KEY, blacklisted_at REAL NOT NULL)"
            )
            conn.commit()
            conn.close()

            bl = TokenBlacklist()
            bl.set_sqlite_path(tmp.name)
            bl.add("jti-fast")

            # Local check returns True without needing DB round-trip
            assert bl.contains("jti-fast")
        finally:
            os.unlink(tmp.name)


class TestTokenBlacklistSQLite:
    """Tests for SQLite-backed token blacklist."""

    def _make_db(self):
        """Create a temp SQLite DB with the token_blacklist table."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_blacklist "
            "(jti TEXT PRIMARY KEY, blacklisted_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()
        return tmp.name

    def test_sqlite_add_and_contains(self):
        """Add via SQLite backend, then verify contains reads from DB."""
        from code_indexer.server.app import TokenBlacklist

        db_path = self._make_db()
        try:
            bl = TokenBlacklist()
            bl.set_sqlite_path(db_path)
            bl.add("jti-sqlite-1")
            assert bl.contains("jti-sqlite-1")
            assert not bl.contains("jti-sqlite-missing")

            # Verify data actually in SQLite
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = ?", ("jti-sqlite-1",)
            ).fetchone()
            conn.close()
            assert row is not None
        finally:
            os.unlink(db_path)

    def test_cross_node_visibility(self):
        """Token blacklisted on node1 is visible on node2 via shared DB."""
        from code_indexer.server.app import TokenBlacklist

        db_path = self._make_db()
        try:
            # Simulate two nodes sharing the same SQLite DB
            node1 = TokenBlacklist()
            node1.set_sqlite_path(db_path)

            node2 = TokenBlacklist()
            node2.set_sqlite_path(db_path)

            # node1 blacklists a token
            node1.add("jti-cross-node")

            # node2 should see it via DB lookup (not in its local set)
            assert node2.contains("jti-cross-node")
        finally:
            os.unlink(db_path)


class TestTokenBlacklistPG:
    """Tests for PostgreSQL-backed token blacklist."""

    def test_pg_add_and_contains(self):
        """PG backend: set_connection_pool stores the pool reference.

        Since we cannot spin up a real PostgreSQL in unit tests, we verify
        that set_connection_pool stores the pool reference. The actual PG
        code paths use psycopg-style %s placeholders and connection() context
        manager, which are integration-tested in cluster E2E tests.
        """
        from code_indexer.server.app import TokenBlacklist

        bl = TokenBlacklist()
        assert bl._pool is None
        sentinel = object()
        bl.set_connection_pool(sentinel)
        assert bl._pool is sentinel

    def test_set_connection_pool(self):
        """set_connection_pool stores the pool reference."""
        from code_indexer.server.app import TokenBlacklist

        bl = TokenBlacklist()
        assert bl._pool is None

        mock_pool = object()
        bl.set_connection_pool(mock_pool)
        assert bl._pool is mock_pool
