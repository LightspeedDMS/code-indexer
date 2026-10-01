"""
Regression test for a bug introduced BY the Bug #1563 fix.

Bug #1563 added a construction-time schema self-heal to
``BackgroundJobsSqliteBackend.__init__`` (``_ensure_executing_pid_column``)
that runs ``PRAGMA table_info(background_jobs)`` and, when the column is
missing, issues ``ALTER TABLE background_jobs ADD COLUMN executing_pid``.

``PRAGMA table_info`` against a table that does not exist at all returns an
EMPTY result set rather than raising -- so "table absent" and "table present
but missing the column" were indistinguishable by that check alone, and both
fell into the ALTER branch. Against a genuinely missing table the ALTER then
raises ``sqlite3.OperationalError: no such table: background_jobs``.

``StorageFactory._create_sqlite_backends()`` (and therefore
``StorageFactory.create_backends()``) constructs ``BackgroundJobsSqliteBackend``
directly against ``data_dir`` with NO prior call to
``DatabaseSchema.initialize_database()`` -- exactly the legitimate pattern
several pre-existing tests use (e.g. ``test_factory_embedding_call_stats_1418.py``,
``test_logs_backend.py``, ``test_payload_cache_backend.py``, ``test_oauth_backend.py``,
``test_query_embedding_cache_1105.py``), all of which construct the full
registry against a fresh ``tmp_path`` to exercise an unrelated backend. Bug
#1563's self-heal broke every one of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestFactoryConstructionAgainstFreshDataDir:
    def test_factory_sqlite_backends_construct_without_prior_schema_init(
        self, tmp_path: Path
    ) -> None:
        """StorageFactory._create_sqlite_backends() must succeed against a
        FRESH data dir where nothing has called
        DatabaseSchema.initialize_database() yet -- the exact path that
        raised ``sqlite3.OperationalError: no such table: background_jobs``
        before the fix."""
        from code_indexer.server.storage.factory import StorageFactory

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))
        try:
            assert registry.background_jobs is not None
        finally:
            registry.background_jobs.close()

    def test_create_backends_public_entry_point_also_succeeds(
        self, tmp_path: Path
    ) -> None:
        """Same regression via the public StorageFactory.create_backends()
        entry point (default storage_mode='sqlite'), matching how the
        previously-failing tests invoke it."""
        from code_indexer.server.storage.factory import StorageFactory

        registry = StorageFactory.create_backends(config={}, data_dir=str(tmp_path))
        try:
            assert registry.background_jobs is not None
        finally:
            registry.background_jobs.close()


class TestExecutingPidColumnStillSelfHealsRealDatabases:
    def test_column_present_after_normal_schema_init_then_factory_construction(
        self, tmp_path: Path
    ) -> None:
        """The fix must not silently disable the migration forever: once
        DatabaseSchema.initialize_database() has actually created the
        background_jobs table (the normal production startup order --
        schema init runs BEFORE StorageFactory.create_backends()), the
        self-heal must still add the executing_pid column."""
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.factory import StorageFactory

        db_path = tmp_path / "cidx_server.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))
        try:
            assert registry.background_jobs is not None

            import sqlite3

            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("PRAGMA table_info(background_jobs)")
                columns = {row[1] for row in cursor.fetchall()}
            finally:
                conn.close()

            assert "executing_pid" in columns
        finally:
            registry.background_jobs.close()

    def test_column_present_on_a_pre_existing_row_before_upgrade(
        self, tmp_path: Path
    ) -> None:
        """Simulates an already-deployed database created before Bug #1563
        shipped (table exists, executing_pid column does not). Constructing
        the backend must self-heal it, exactly as designed."""
        import sqlite3

        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        db_path = tmp_path / "cidx_server.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        # Sanity: the base schema does NOT define executing_pid.
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(background_jobs)")
            columns_before = {row[1] for row in cursor.fetchall()}
        finally:
            conn.close()
        assert "executing_pid" not in columns_before

        backend = BackgroundJobsSqliteBackend(str(db_path))
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("PRAGMA table_info(background_jobs)")
                columns_after = {row[1] for row in cursor.fetchall()}
            finally:
                conn.close()
            assert "executing_pid" in columns_after
        finally:
            backend.close()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
