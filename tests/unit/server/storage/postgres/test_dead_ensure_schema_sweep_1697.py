"""Regression tests for Issue #1697: systematic sweep of the dead
`_ensure_schema()` pattern already fixed twice (Bug #1655 wiki_cache_backend.py,
Bug #1662 diagnostics_backend.py).

`startup/service_init.py` always runs `MigrationRunner` (fail-fast on error)
BEFORE `StorageFactory.create_backends()` constructs any PostgreSQL backend
in `storage_mode=postgres`. Each backend covered here is constructed ONLY
from that single factory call site (or, for `TemporalMetadataPostgresBackend`,
only from a factory installed in `startup/lifespan.py` AFTER the same
backend_registry has already been built post-migration) -- so their
self-heal `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` blocks
never actually run against a real database in any live deployment. Their
only real effect was being second, silently-drifting sources of truth for
schema already owned by the SQL migrations. This file locks in that the
dead blocks are removed entirely (mirroring the Bug #1655/#1662 precedent)
rather than re-synced.

Tests mock ConnectionPool (no real PostgreSQL required -- same pattern as
test_wiki_cache_backend_metadata_column_bug_1655.py /
test_diagnostics_backend_dead_schema_1662.py: pool.connection() -> conn
context manager, conn.execute(sql, params) returns a cursor-like mock).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def _make_mock_pool() -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> Any:
    return pool.connection.return_value.__enter__.return_value


class TestLogsPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )

        pool = _make_mock_pool()
        LogsPostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestQueryEmbeddingCachePostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
            QueryEmbeddingCachePostgresBackend,
        )

        pool = _make_mock_pool()
        QueryEmbeddingCachePostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestResearchSessionsPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.research_sessions_backend import (
            ResearchSessionsPostgresBackend,
        )

        pool = _make_mock_pool()
        ResearchSessionsPostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestSCIPAuditPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.scip_audit_backend import (
            SCIPAuditPostgresBackend,
        )

        pool = _make_mock_pool()
        SCIPAuditPostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestTemporalMetadataPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_mock_pool()
        TemporalMetadataPostgresBackend(pool, collection_key="abc123")

        assert _get_conn(pool).execute.call_count == 0


class TestRefreshTokenPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.refresh_token_backend import (
            RefreshTokenPostgresBackend,
        )

        pool = _make_mock_pool()
        RefreshTokenPostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestSelfMonitoringPostgresBackendConstructorIsDbFree:
    def test_zero_conn_execute_calls_at_construction(self):
        from code_indexer.server.storage.postgres.self_monitoring_backend import (
            SelfMonitoringPostgresBackend,
        )

        pool = _make_mock_pool()
        SelfMonitoringPostgresBackend(pool)

        assert _get_conn(pool).execute.call_count == 0


class TestOAuthPostgresBackendConstructorSeedsClientCredentialsOnly:
    """OAuthPostgresBackend is a PARTIAL keep, not a full deletion: its dead
    CREATE TABLE/CREATE INDEX statements are covered by migration
    025_runtime_only_tables.sql, but its `client_credentials` synthetic
    seed-row INSERT (ON CONFLICT DO NOTHING) is genuinely live -- no
    migration replicates it, and OAuthManager's own equivalent seeding path
    (oauth_manager.py's `_init_database`) is skipped entirely whenever a
    `storage_backend` is supplied (the postgres/cluster construction path).
    Without this seed row, `oauth_tokens.client_id = 'client_credentials'`
    would violate its FK constraint and the client_credentials OAuth grant
    would be broken cluster-wide.
    """

    def test_constructor_issues_exactly_one_seed_insert(self):
        from code_indexer.server.storage.postgres.oauth_backend import (
            OAuthPostgresBackend,
        )

        pool = _make_mock_pool()
        OAuthPostgresBackend(pool)

        conn = _get_conn(pool)
        assert conn.execute.call_count == 1
        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO oauth_clients" in sql
        assert "ON CONFLICT (client_id) DO NOTHING" in sql
        assert params[0] == "client_credentials"

    def test_constructor_issues_zero_create_table_statements(self):
        from code_indexer.server.storage.postgres.oauth_backend import (
            OAuthPostgresBackend,
        )

        pool = _make_mock_pool()
        OAuthPostgresBackend(pool)

        conn = _get_conn(pool)
        create_statements = [
            call.args[0]
            for call in conn.execute.call_args_list
            if "CREATE" in call.args[0]
        ]
        assert create_statements == []
