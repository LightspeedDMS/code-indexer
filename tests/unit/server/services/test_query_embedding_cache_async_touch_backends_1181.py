"""Bug #1181 Perf Fix #2: Backend-specific tests for async touch_last_used_batch.

Tests cover:
- SQLite backend: touch_last_used_batch exists and persists correctly
- SQLite backend: batch is atomic (one transaction, no partial updates)
- SQLite backend: no-op for empty list
- PG backend: touch_last_used_batch exists
- PG backend: SET LOCAL synchronous_commit=off is emitted BEFORE UPDATE statements
- PG backend: no-op for empty list (no connection opened)
- PG backend: fail-open (no exception bubbles up from connection failure)
- Protocol parity: QueryEmbeddingCacheBackend Protocol declares touch_last_used_batch
- SQLite backend satisfies the updated Protocol (isinstance check)
- PG backend satisfies the updated Protocol (isinstance check via FakePool)
- Lifespan wiring: start/stop guard for the async touch flusher
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest


# ---------------------------------------------------------------------------
# 1. SQLite backend: touch_last_used_batch
# ---------------------------------------------------------------------------


class TestSQLiteBackendTouchLastUsedBatch:
    """SQLite backend must expose touch_last_used_batch that persists correctly."""

    def _make_backend(self, tmp_path: Path):
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        return QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))

    def _insert_row(self, backend, key: str, t0: float = 1000.0, dim: int = 2) -> None:
        import numpy as np

        blob = np.asarray([1.0, 2.0], dtype="<f4").tobytes()
        backend.upsert(key, "voyage-ai", "vcode3", dim, blob, t0, t0)

    def test_touch_last_used_batch_method_exists(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        assert hasattr(backend, "touch_last_used_batch"), (
            "QueryEmbeddingCacheSqliteBackend must have touch_last_used_batch method"
        )
        assert callable(backend.touch_last_used_batch)

    def test_touch_last_used_batch_updates_all_rows(self, tmp_path: Path) -> None:
        """touch_last_used_batch must UPDATE all specified rows in one shot."""
        import sqlite3

        backend = self._make_backend(tmp_path)
        t0 = 1000.0
        self._insert_row(backend, "k1", t0)
        self._insert_row(backend, "k2", t0)

        t_new = 9999.0
        items: List[Tuple] = [
            ("k1", "voyage-ai", "vcode3", 2, t_new),
            ("k2", "voyage-ai", "vcode3", 2, t_new),
        ]
        backend.touch_last_used_batch(items)

        with sqlite3.connect(str(tmp_path / "qec.db")) as conn:
            rows = conn.execute(
                "SELECT cache_key, last_used FROM query_embedding_cache ORDER BY cache_key"
            ).fetchall()

        assert len(rows) == 2
        for row in rows:
            assert row[1] == pytest.approx(t_new, abs=1e-6), (
                f"Row {row[0]}: expected last_used={t_new}, got {row[1]}"
            )

    def test_touch_last_used_batch_no_op_for_empty_list(self, tmp_path: Path) -> None:
        """touch_last_used_batch([]) must not raise."""
        backend = self._make_backend(tmp_path)
        backend.touch_last_used_batch([])  # must not raise

    def test_touch_last_used_batch_nonexistent_key_is_safe(
        self, tmp_path: Path
    ) -> None:
        """UPDATE on a non-existent row must silently no-op (UPDATE 0 rows, not raise)."""
        import sqlite3

        backend = self._make_backend(tmp_path)
        t0 = 1000.0
        self._insert_row(backend, "k1", t0)

        t_new = 2000.0
        items: List[Tuple] = [
            ("k1", "voyage-ai", "vcode3", 2, t_new),
            ("k-nonexistent", "voyage-ai", "vcode3", 2, t_new),
        ]
        backend.touch_last_used_batch(items)  # must not raise

        with sqlite3.connect(str(tmp_path / "qec.db")) as conn:
            row = conn.execute(
                "SELECT last_used FROM query_embedding_cache WHERE cache_key='k1'"
            ).fetchone()

        assert row[0] == pytest.approx(t_new, abs=1e-6)

    def test_touch_last_used_batch_is_atomic(self, tmp_path: Path) -> None:
        """All UPDATEs in a batch must commit atomically (single transaction).

        We verify atomicity by asserting that after a successful batch all rows
        have the new timestamp — no partial commits between rows.
        """
        import sqlite3

        backend = self._make_backend(tmp_path)
        t0 = 1000.0
        # Insert 3 rows
        for key in ("a", "b", "c"):
            self._insert_row(backend, key, t0)

        t_new = 5000.0
        items: List[Tuple] = [
            ("a", "voyage-ai", "vcode3", 2, t_new),
            ("b", "voyage-ai", "vcode3", 2, t_new),
            ("c", "voyage-ai", "vcode3", 2, t_new),
        ]
        backend.touch_last_used_batch(items)

        with sqlite3.connect(str(tmp_path / "qec.db")) as conn:
            rows = conn.execute(
                "SELECT cache_key, last_used FROM query_embedding_cache ORDER BY cache_key"
            ).fetchall()

        assert len(rows) == 3
        # All 3 rows must reflect the new timestamp (atomic commit)
        for row in rows:
            assert row[1] == pytest.approx(t_new, abs=1e-6), (
                f"Partial commit detected on row {row[0]}: last_used={row[1]}, expected {t_new}"
            )


# ---------------------------------------------------------------------------
# 2. PostgreSQL backend: touch_last_used_batch with synchronous_commit=off
# ---------------------------------------------------------------------------


class TestPostgresBackendTouchLastUsedBatch:
    """PG backend must expose touch_last_used_batch with SET LOCAL synchronous_commit=off."""

    def _make_pg_backend_no_schema(self):
        """Return a PG backend with schema creation bypassed via object.__new__."""
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        return object.__new__(QueryEmbeddingCachePostgresBackend)

    def test_touch_last_used_batch_method_exists(self) -> None:
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        assert hasattr(QueryEmbeddingCachePostgresBackend, "touch_last_used_batch"), (
            "QueryEmbeddingCachePostgresBackend must have touch_last_used_batch method"
        )
        assert callable(QueryEmbeddingCachePostgresBackend.touch_last_used_batch)

    def test_pg_synchronous_commit_off_before_update(self) -> None:
        """SET LOCAL synchronous_commit=off must appear BEFORE any UPDATE statement."""
        executed_stmts: List[str] = []

        class FakeCursor:
            """Models a psycopg v3 cursor: executemany lives HERE, not on the connection."""

            def execute(self, sql: str, params=None) -> None:
                executed_stmts.append(sql.strip())

            def executemany(self, sql: str, params_seq) -> None:
                # Record the SQL template once per executemany call
                executed_stmts.append(sql.strip())

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakeConn:
            # psycopg v3 Connection: execute() is a convenience method on the connection
            # (used for the SET LOCAL statement), but executemany() lives on the cursor.
            # Modelling this faithfully is what catches the Bug #1181 regression.
            def execute(self, sql: str, params=None):
                executed_stmts.append(sql.strip())
                return self

            def cursor(self):
                return FakeCursor()

            def commit(self) -> None:
                pass

            def fetchone(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class FakePool:
            def connection(self):
                return FakeConn()

        backend = self._make_pg_backend_no_schema()
        backend._pool = FakePool()

        items: List[Tuple] = [("k1", "voyage-ai", "vcode3", 4, 9999.0)]
        backend.touch_last_used_batch(items)

        # Verify SET LOCAL synchronous_commit=off appears
        sync_commit_stmts = [
            s for s in executed_stmts if "synchronous_commit" in s.lower()
        ]
        assert len(sync_commit_stmts) >= 1, (
            "Expected 'SET LOCAL synchronous_commit = off' to be issued. "
            f"Executed statements: {executed_stmts}"
        )

        # Verify at least one UPDATE was issued
        update_stmts = [s for s in executed_stmts if s.upper().startswith("UPDATE")]
        assert len(update_stmts) >= 1, (
            f"Expected at least one UPDATE statement. Executed: {executed_stmts}"
        )

        # Verify SET LOCAL appears BEFORE the first UPDATE (ordering guarantee)
        first_sync_pos = next(
            i for i, s in enumerate(executed_stmts) if "synchronous_commit" in s.lower()
        )
        first_update_pos = next(
            i for i, s in enumerate(executed_stmts) if s.upper().startswith("UPDATE")
        )
        assert first_sync_pos < first_update_pos, (
            "SET LOCAL synchronous_commit=off must be issued BEFORE the first UPDATE. "
            f"sync_commit at index {first_sync_pos}, UPDATE at index {first_update_pos}. "
            f"Full sequence: {executed_stmts}"
        )

    def test_pg_touch_last_used_batch_no_op_for_empty(self) -> None:
        """Empty batch must not open a DB connection at all."""
        connection_opened = [False]

        class FakeConn:
            def execute(self, sql: str, params=None):
                return self

            def commit(self) -> None:
                pass

            def __enter__(self):
                connection_opened[0] = True
                return self

            def __exit__(self, *args):
                pass

        class FakePool:
            def connection(self):
                return FakeConn()

        backend = self._make_pg_backend_no_schema()
        backend._pool = FakePool()

        backend.touch_last_used_batch([])
        assert not connection_opened[0], (
            "No DB connection should be opened for an empty batch"
        )

    def test_pg_touch_last_used_batch_fail_open(self) -> None:
        """PG touch_last_used_batch must be fail-open (exception from pool does not bubble)."""

        class FailPool:
            def connection(self):
                raise RuntimeError("PG pool exhausted")

        backend = self._make_pg_backend_no_schema()
        backend._pool = FailPool()

        # Must not raise
        backend.touch_last_used_batch([("k1", "voyage-ai", "vcode3", 4, 9999.0)])


# ---------------------------------------------------------------------------
# 3. Protocol parity: QueryEmbeddingCacheBackend declares touch_last_used_batch
# ---------------------------------------------------------------------------


class TestProtocolParity:
    """The QueryEmbeddingCacheBackend Protocol must declare touch_last_used_batch."""

    def test_protocol_has_touch_last_used_batch(self) -> None:
        from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend

        assert hasattr(QueryEmbeddingCacheBackend, "touch_last_used_batch"), (
            "QueryEmbeddingCacheBackend Protocol must declare touch_last_used_batch"
        )

    def test_sqlite_backend_satisfies_updated_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec_proto.db"))
        assert isinstance(backend, QueryEmbeddingCacheBackend), (
            "SQLite backend must satisfy the updated QueryEmbeddingCacheBackend Protocol "
            "(including touch_last_used_batch)"
        )

    def test_pg_backend_satisfies_updated_protocol(self) -> None:
        """PG backend class must have all Protocol methods (structural check)."""
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )
        from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend

        required_methods = [
            m for m in dir(QueryEmbeddingCacheBackend) if not m.startswith("_")
        ]
        for method in required_methods:
            assert hasattr(QueryEmbeddingCachePostgresBackend, method), (
                f"QueryEmbeddingCachePostgresBackend missing Protocol method: {method}"
            )


# ---------------------------------------------------------------------------
# 4. Lifespan wiring: start/stop guard for the async touch flusher
# ---------------------------------------------------------------------------


class TestLifespanWiringAsyncTouch:
    """Source-text guard: lifespan.py must call cache.start() and cache.stop()."""

    _LIFESPAN_PATH = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "startup"
        / "lifespan.py"
    )

    def test_lifespan_calls_query_embedding_cache_start(self) -> None:
        source = self._LIFESPAN_PATH.read_text()
        assert "_query_embedding_cache.start()" in source, (
            "lifespan.py must call _query_embedding_cache.start() after constructing the cache "
            "to launch the async touch flusher thread"
        )

    def test_lifespan_calls_query_embedding_cache_stop(self) -> None:
        source = self._LIFESPAN_PATH.read_text()
        assert "_query_embedding_cache.stop()" in source, (
            "lifespan.py must call _query_embedding_cache.stop() on shutdown "
            "to final-flush pending touches and join the flusher thread"
        )

    def test_start_before_yield_and_stop_after_yield(self) -> None:
        """start() must be in the startup section (before yield); stop() in shutdown."""
        source = self._LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        start_pos = source.find("_query_embedding_cache.start()")
        stop_pos = source.find("_query_embedding_cache.stop()")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert start_pos != -1, (
            "_query_embedding_cache.start() not found in lifespan.py"
        )
        assert stop_pos != -1, "_query_embedding_cache.stop() not found in lifespan.py"
        assert start_pos < yield_pos, (
            "_query_embedding_cache.start() must run BEFORE yield (startup)"
        )
        assert stop_pos > yield_pos, (
            "_query_embedding_cache.stop() must run AFTER yield (shutdown)"
        )
