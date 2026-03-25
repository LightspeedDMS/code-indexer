"""
Tests for LogsPostgresBackend (Story #501 AC1).

Tests the PostgreSQL backend for operational log storage.

PostgreSQL-dependent tests are skipped when psycopg is not installed or
when no PostgreSQL connection is available (CI without a real PG instance).

Protocol satisfaction test does NOT require psycopg because it only checks
the class structure, not runtime behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_if_available():
    """Return a ConnectionPool connected to a test database, or None."""
    if not HAS_PSYCOPG:
        return None
    import os

    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return None
    try:
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

        pool = ConnectionPool(dsn)
        # Quick smoke test
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return pool
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AC1: Protocol satisfaction (no psycopg required)
# ---------------------------------------------------------------------------


class TestLogsPostgresBackendProtocol:
    """LogsPostgresBackend must satisfy the LogsBackend Protocol."""

    def test_logs_postgres_backend_satisfies_protocol(self):
        """LogsPostgresBackend must implement all LogsBackend protocol methods.

        This test verifies the class structure matches the protocol without
        needing a live PostgreSQL connection.
        """
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        # Verify all protocol methods are present on the class
        required_methods = ["insert_log", "query_logs", "cleanup_old_logs", "close"]
        for method in required_methods:
            assert hasattr(
                LogsPostgresBackend, method
            ), f"LogsPostgresBackend must have method: {method}"

        # Verify the class is structurally compatible with the protocol
        protocol_attrs = None
        try:
            from code_indexer.server.storage.protocols import LogsBackend

            protocol_attrs = getattr(LogsBackend, "__protocol_attrs__", None)
        except ImportError:
            pass

        if protocol_attrs:
            missing = set(protocol_attrs) - set(dir(LogsPostgresBackend))
            assert (
                not missing
            ), f"LogsPostgresBackend is missing protocol attributes: {missing}"

    def test_logs_postgres_backend_has_close_noop(self):
        """close() method must exist (pool lifecycle is managed externally)."""
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        assert callable(
            getattr(LogsPostgresBackend, "close", None)
        ), "LogsPostgresBackend must have a callable close() method"

    def test_logs_postgres_backend_is_imported_by_factory(self):
        """StorageFactory must be able to import LogsPostgresBackend lazily."""
        # This verifies the import path is correct without needing a live PG.
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        assert LogsPostgresBackend is not None

    def test_factory_postgres_path_references_logs_postgres_backend(self):
        """StorageFactory._create_postgres_backends must reference LogsPostgresBackend."""
        import inspect
        from code_indexer.server.storage import factory

        source = inspect.getsource(factory.StorageFactory._create_postgres_backends)
        assert "LogsPostgresBackend" in source, (
            "StorageFactory._create_postgres_backends must use LogsPostgresBackend, "
            "not the SQLite fallback"
        )


# ---------------------------------------------------------------------------
# AC2: Live PostgreSQL tests (skipped when psycopg unavailable or no DSN)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_pool():
    """Module-scoped ConnectionPool for live PG tests."""
    pool = _make_pool_if_available()
    if pool is None:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    yield pool
    pool.close()


@pytest.fixture
def backend(pg_pool):
    """Fresh LogsPostgresBackend for each test.

    Truncates the logs table before each test to ensure isolation.
    """
    from code_indexer.server.storage.postgres.logs_backend import LogsPostgresBackend

    b = LogsPostgresBackend(pg_pool)
    # Truncate table for isolation
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE TABLE logs RESTART IDENTITY")
        conn.commit()
    yield b
    b.close()


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
class TestLogsPostgresBackendLive:
    """Live PostgreSQL tests - require TEST_POSTGRES_DSN environment variable."""

    def test_insert_log_writes_to_postgres(self, backend, pg_pool):
        """insert_log() must persist a record readable via query_logs()."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="test.source",
            message="Hello from PostgreSQL",
            correlation_id="corr-pg-001",
            user_id="user-pg",
            request_path="/api/pg-test",
            extra_data='{"backend": "postgres"}',
            node_id="node-pg-1",
        )

        results, total = backend.query_logs(limit=10, offset=0)

        assert total == 1
        assert len(results) == 1
        row = results[0]
        assert row["level"] == "INFO"
        assert row["source"] == "test.source"
        assert row["message"] == "Hello from PostgreSQL"
        assert row["correlation_id"] == "corr-pg-001"
        assert row["user_id"] == "user-pg"
        assert row["request_path"] == "/api/pg-test"
        assert row["node_id"] == "node-pg-1"

    def test_query_logs_filters_by_level(self, backend):
        """query_logs(level=...) must return only matching-level records."""
        ts = datetime.now(timezone.utc).isoformat()
        for lvl in ("ERROR", "INFO", "WARNING"):
            backend.insert_log(
                timestamp=ts,
                level=lvl,
                source="svc",
                message=f"msg {lvl}",
                node_id=None,
            )

        results, total = backend.query_logs(level="ERROR", limit=100, offset=0)

        assert total == 1
        assert results[0]["level"] == "ERROR"

    def test_query_logs_filters_by_node_id(self, backend):
        """query_logs(node_id=...) must return only records for that node."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts, level="INFO", source="svc", message="From A", node_id="node-A"
        )
        backend.insert_log(
            timestamp=ts, level="INFO", source="svc", message="From B", node_id="node-B"
        )
        backend.insert_log(
            timestamp=ts, level="INFO", source="svc", message="No node", node_id=None
        )

        results, total = backend.query_logs(node_id="node-A", limit=100, offset=0)

        assert total == 1
        assert results[0]["node_id"] == "node-A"
        assert results[0]["message"] == "From A"

    def test_query_logs_pagination(self, backend):
        """query_logs must return correct (list, total_count) with pagination."""
        ts = datetime.now(timezone.utc).isoformat()
        for i in range(5):
            backend.insert_log(
                timestamp=ts,
                level="INFO",
                source="svc",
                message=f"msg {i}",
                node_id=None,
            )

        # Page 1
        results, total = backend.query_logs(limit=3, offset=0)
        assert total == 5
        assert len(results) == 3

        # Page 2
        results2, total2 = backend.query_logs(limit=3, offset=3)
        assert total2 == 5
        assert len(results2) == 2

    def test_query_logs_returns_required_fields(self, backend):
        """Each dict returned by query_logs must contain all required fields."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts,
            level="DEBUG",
            source="test",
            message="Field check",
            correlation_id="c1",
            user_id="u1",
            request_path="/x",
            extra_data=None,
            node_id="n1",
        )

        results, total = backend.query_logs(limit=10, offset=0)

        assert total == 1
        row = results[0]
        required = {
            "id",
            "timestamp",
            "level",
            "source",
            "message",
            "correlation_id",
            "user_id",
            "request_path",
            "node_id",
        }
        missing = required - set(row.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_cleanup_old_logs_removes_old_records(self, backend):
        """cleanup_old_logs() must delete records older than days_to_keep."""
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=40)).isoformat()
        recent_ts = (now - timedelta(days=1)).isoformat()

        backend.insert_log(
            timestamp=old_ts, level="INFO", source="svc", message="Old", node_id=None
        )
        backend.insert_log(
            timestamp=recent_ts,
            level="INFO",
            source="svc",
            message="Recent",
            node_id=None,
        )

        deleted = backend.cleanup_old_logs(days_to_keep=30)

        assert deleted == 1

        results, total = backend.query_logs(limit=100, offset=0)
        assert total == 1
        assert results[0]["message"] == "Recent"

    def test_insert_log_failure_does_not_raise(self, backend, pg_pool):
        """insert_log must swallow failures gracefully (never crash the app)."""
        original_connection = pg_pool.connection

        class _BrokenCtxMgr:
            def __enter__(self):
                raise RuntimeError("Simulated DB failure")

            def __exit__(self, *args):
                pass

        pg_pool.connection = lambda: _BrokenCtxMgr()
        try:
            # Must not raise
            backend.insert_log(
                timestamp="2025-01-01T00:00:00+00:00",
                level="INFO",
                source="svc",
                message="should not crash",
                node_id=None,
            )
        finally:
            pg_pool.connection = original_connection

    def test_close_is_noop(self, backend):
        """close() must not raise any exception."""
        backend.close()  # Should complete silently
