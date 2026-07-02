"""
Unit tests for DatabaseSchema._migrate_background_jobs_job_tracker.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC8: schema migration adds progress_info, metadata columns and indexes
"""

import sqlite3

import pytest


class TestSchemaMigration:
    """Tests for _migrate_background_jobs_job_tracker (AC8)."""

    @pytest.fixture
    def old_db(self, tmp_path):
        """Create a DB with the old schema (no progress_info, no metadata columns)."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """CREATE TABLE background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT
        )"""
        )
        conn.commit()
        conn.close()
        return str(db)

    def test_migration_adds_progress_info_column(self, old_db):
        """
        _migrate_background_jobs_job_tracker adds the progress_info column.

        Given a database without progress_info
        When the migration method is run
        Then progress_info column exists in background_jobs
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        conn = sqlite3.connect(old_db)
        schema = DatabaseSchema()
        schema._migrate_background_jobs_job_tracker(conn)

        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "progress_info" in columns

    def test_migration_adds_metadata_column(self, old_db):
        """
        _migrate_background_jobs_job_tracker adds the metadata column.

        Given a database without metadata
        When the migration method is run
        Then metadata column exists in background_jobs
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        conn = sqlite3.connect(old_db)
        schema = DatabaseSchema()
        schema._migrate_background_jobs_job_tracker(conn)

        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "metadata" in columns

    def test_migration_creates_indexes(self, old_db):
        """
        _migrate_background_jobs_job_tracker creates all three expected indexes.

        Given a database lacking the job tracker indexes
        When the migration method is run
        Then all three indexes exist
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        conn = sqlite3.connect(old_db)
        schema = DatabaseSchema()
        schema._migrate_background_jobs_job_tracker(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='background_jobs'"
        )
        index_names = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "idx_background_jobs_op_repo_status" in index_names
        assert "idx_background_jobs_user_created" in index_names
        assert "idx_background_jobs_created" in index_names

    def test_migration_is_idempotent(self, old_db):
        """
        Running _migrate_background_jobs_job_tracker twice does not raise an error.

        Given the migration has already been applied
        When the migration is run again
        Then no exception is raised and the schema is still valid
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema()

        conn = sqlite3.connect(old_db)
        schema._migrate_background_jobs_job_tracker(conn)
        # Second run must not raise
        schema._migrate_background_jobs_job_tracker(conn)

        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "progress_info" in columns
        assert "metadata" in columns
