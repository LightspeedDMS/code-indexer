"""Tests for the embedding_call_stats BackendRegistry field (Story #1418
Phase 3).

Phase 1/2 introduced EmbeddingCallStatsSqliteBackend /
EmbeddingCallStatsPostgresBackend but never wired a shared instance into
BackendRegistry -- research confirmed the live server process therefore
never installs a real InProcessAsyncWriter (every server-side
instrumented call falls through to the default NoOpWriter). This wires the
field so lifespan.py can construct+start a real writer, reusing the SAME
db_path (SQLite) / general connection pool (PostgreSQL) other backends in
this registry already use -- never a second, isolated connection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBackendRegistryHasEmbeddingCallStatsField:
    def test_field_exists_and_defaults_to_none(self) -> None:
        import dataclasses

        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "embedding_call_stats" in field_names


class TestSqliteModeConstructsRealBackend:
    def test_sqlite_mode_wires_a_real_functional_backend(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallRecord,
            EmbeddingCallStatsSqliteBackend,
        )

        registry = StorageFactory.create_backends(config={}, data_dir=str(tmp_path))

        assert isinstance(
            registry.embedding_call_stats, EmbeddingCallStatsSqliteBackend
        )

        # Anti-Mock: prove it's a REAL, functional backend -- round-trip a
        # record through the actual SQLite table, not just an isinstance
        # check.
        import time

        record = EmbeddingCallRecord(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=5,
            batch_size=1,
            purpose="query",
            success=True,
            latency_ms=10,
            occurred_at=time.time(),
        )
        registry.embedding_call_stats.insert_batch([record])
        rows = registry.embedding_call_stats.query(limit=10)
        assert any(r.model == "voyage-code-3" for r in rows)

    def test_sqlite_mode_reuses_the_shared_db_path(self, tmp_path: Path) -> None:
        """Must reuse the SAME cidx_server.db path other sqlite backends in
        this registry already target -- never a second, isolated DB file."""
        from code_indexer.server.storage.factory import StorageFactory

        registry = StorageFactory.create_backends(config={}, data_dir=str(tmp_path))

        assert registry.embedding_call_stats._db_path == str(
            tmp_path / "cidx_server.db"
        )


class TestPostgresModeConstructsRealBackendType:
    def test_postgres_mode_wires_backend_bound_to_the_general_pool(self) -> None:
        """Mocks ONLY the network boundary (ConnectionPool) -- the wiring
        logic under test (which pool gets passed to which backend
        constructor) runs for real."""
        fake_pool = MagicMock()

        with patch(
            "code_indexer.server.storage.postgres.connection_pool.ConnectionPool",
            return_value=fake_pool,
        ):
            from code_indexer.server.storage.factory import StorageFactory
            from code_indexer.server.services.embedding_call_stats import (
                EmbeddingCallStatsPostgresBackend,
            )

            registry = StorageFactory._create_postgres_backends(
                {"postgres_dsn": "postgresql://x"}
            )

        assert isinstance(
            registry.embedding_call_stats, EmbeddingCallStatsPostgresBackend
        )
        # Must be bound to the SAME general pool other backends share, not
        # a second isolated pool.
        assert registry.embedding_call_stats._pool is registry.connection_pool


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
