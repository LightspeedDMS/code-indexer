"""Story #1293: DatabaseSchema.initialize_database() must create the
search_embed_event table (SQLite solo-mode bootstrap path), mirroring the
existing search_event_log / query_analytics_exports precedent.
"""

import sqlite3

import pytest


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "cidx_server.db")


class TestSearchEmbedEventSchemaBootstrap:
    def test_initialize_database_creates_search_embed_event_table(self, temp_db_path):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(temp_db_path)
        schema.initialize_database()

        conn = sqlite3.connect(temp_db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(search_embed_event)"
                ).fetchall()
            }
        finally:
            conn.close()

        expected = {
            "id",
            "timestamp",
            "correlation_id",
            "node_id",
            "provider",
            "model",
            "config_digest",
            "cache_mode",
            "outcome",
            "role",
            "live_batch_id",
            "embed_key",
            "long_key",
            "latency_ms",
            "shadow_cosine",
            "audit_sampled",
            "audit_cosine",
        }
        assert expected.issubset(cols)

    def test_initialize_database_creates_search_embed_event_indexes(self, temp_db_path):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(temp_db_path)
        schema.initialize_database()

        conn = sqlite3.connect(temp_db_path)
        try:
            idx_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='search_embed_event'"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "idx_see_timestamp" in idx_names
        assert "idx_see_correlation_id" in idx_names

    def test_initialize_database_is_idempotent(self, temp_db_path):
        """Running initialize_database() twice must not raise (additive-only,
        rolling-restart safe per CLAUDE.md)."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(temp_db_path)
        schema.initialize_database()
        schema.initialize_database()  # must not raise
