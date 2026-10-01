"""RED-first tests for Bug #1553: LogsPostgresBackend.query_logs() capability
parity (PostgreSQL half -- SQLite half lives in
test_logs_backend_query_extension_1553.py).

Gated on a real TEST_POSTGRES_DSN, mirroring the existing gating pattern in
test_logs_postgres_backend.py (broad except around the connectivity probe is
intentional and matches that precedent: a DSN can fail to connect for many
reasons -- network, auth, DNS -- across different CI/dev environments, and
any such failure means "treat as unavailable, skip", not "test error"). The
exception is logged at debug level so a misconfigured DSN is still
discoverable, rather than silently swallowed. No mocking of the database
driver.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False

logger = logging.getLogger(__name__)


def _make_pg_pool_if_available():
    if not HAS_PSYCOPG:
        return None
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return None
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = None
    try:
        pool = ConnectionPool(dsn)
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return pool
    except Exception:
        logger.debug(
            "TEST_POSTGRES_DSN configured but unreachable; skipping PostgreSQL "
            "tests for this run.",
            exc_info=True,
        )
        if pool is not None:
            pool.close()
        return None


@pytest.fixture
def pg_pool():
    pool = _make_pg_pool_if_available()
    if pool is None:
        pytest.skip("No real PostgreSQL test database available (TEST_POSTGRES_DSN)")
    try:
        yield pool
    finally:
        pool.close()


class TestPostgresBackendIsCrossNode:
    def test_class_declares_cross_node_true(self):
        # Class-level check -- no live connection required.
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        assert LogsPostgresBackend.is_cross_node_backend is True


class TestPostgresBackendInsertLogBatchReturnValue:
    def test_returns_true_on_success_and_false_on_real_failure(self, pg_pool):
        """Bug #1553: insert_log_batch must report a real bool outcome so
        SQLiteLogHandler's writer loop can detect failure -- today it always
        returns None (implicit), so a real production insert failure was
        completely invisible to the writer loop's observability logic.

        The failure case uses a deliberately malformed 9-element tuple
        (schema requires 10) to trigger a genuine psycopg error inside the
        real executemany call -- no mocking.
        """
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        backend = LogsPostgresBackend(pg_pool)

        success_result = backend.insert_log_batch(
            [
                (
                    "2026-01-01T00:00:00Z",
                    "INFO",
                    "mod.a",
                    "bug1553-insert-batch-ok",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]
        )
        assert success_result is True

        # Malformed: only 9 elements, schema/query expects 10.
        failure_result = backend.insert_log_batch(
            [
                (
                    "2026-01-01T00:00:00Z",
                    "INFO",
                    "mod.a",
                    "bad",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]
        )
        assert failure_result is False

        with pg_pool.connection() as conn:
            conn.execute(
                "DELETE FROM logs WHERE message = %s",
                ("bug1553-insert-batch-ok",),
            )
            conn.commit()


class TestPostgresBackendLevelsSearchSortOrder:
    def test_levels_and_search_and_sort_order(self, pg_pool):
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        backend = LogsPostgresBackend(pg_pool)
        # Unique per-run marker: safe for concurrent/repeated runs against a
        # shared test database (a fixed string could collide with leftover
        # rows from another concurrent run and corrupt both total == 2 and
        # the cleanup DELETE).
        marker = f"bug1553-{uuid.uuid4().hex}"
        try:
            backend.insert_log(
                timestamp="2026-01-01T00:00:00Z",
                level="ERROR",
                source="mod.a",
                message=f"{marker} disk failure detected",
            )
            backend.insert_log(
                timestamp="2026-01-01T00:00:01Z",
                level="WARNING",
                source="mod.b",
                message=f"{marker} cache miss for needle-token",
            )
            backend.insert_log(
                timestamp="2026-01-01T00:00:02Z",
                level="INFO",
                source="mod.c",
                message=f"{marker} startup complete",
            )

            results, total = backend.query_logs(
                levels=["ERROR", "WARNING"], search=marker, limit=100
            )
            assert total == 2
            assert {r["level"] for r in results} == {"ERROR", "WARNING"}

            search_results, search_total = backend.query_logs(
                search=marker.upper(), limit=100
            )
            assert search_total == 3
            assert any("needle-token" in r["message"].lower() for r in search_results)

            asc_results, _ = backend.query_logs(
                search=marker, sort_order="asc", limit=100
            )
            assert asc_results[0]["message"] == f"{marker} disk failure detected"
        finally:
            with pg_pool.connection() as conn:
                conn.execute("DELETE FROM logs WHERE message LIKE %s", (f"%{marker}%",))
                conn.commit()
