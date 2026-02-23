"""
Unit tests for database_manager.py - SQLite database connection pooling and schema management.

Tests written FIRST following TDD methodology.
Story #702: Migrate Central JSON Files to SQLite
"""

import sqlite3
from pathlib import Path

import pytest


class TestDatabaseSchema:
    """Tests for DatabaseSchema class that creates and manages SQLite tables."""

    def test_database_schema_creates_all_required_tables(self, tmp_path: Path) -> None:
        """
        Given a fresh database path
        When DatabaseSchema.initialize_database() is called
        Then all required tables are created with correct structure.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        # Verify all tables exist
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        expected_tables = [
            "background_jobs",
            "ci_tokens",
            "dependency_map_tracking",
            "description_refresh_tracking",
            "diagnostic_results",
            "global_repos",
            "golden_repos_metadata",
            "invalidated_sessions",
            "password_change_timestamps",
            "repo_categories",
            "research_messages",
            "research_sessions",
            "self_monitoring_issues",
            "self_monitoring_scans",
            "sqlite_sequence",
            "ssh_key_hosts",
            "ssh_keys",
            "sync_jobs",
            "user_api_keys",
            "user_mcp_credentials",
            "user_oidc_identities",
            "users",
        ]
        assert sorted(tables) == sorted(expected_tables)

    def test_database_schema_global_repos_table_structure(self, tmp_path: Path) -> None:
        """
        Given an initialized database
        When we inspect global_repos table
        Then it has correct columns with proper types.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(global_repos)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "alias_name": "TEXT",
            "repo_name": "TEXT",
            "repo_url": "TEXT",
            "index_path": "TEXT",
            "created_at": "TEXT",
            "last_refresh": "TEXT",
            "enable_temporal": "BOOLEAN",
            "temporal_options": "TEXT",
            "enable_scip": "BOOLEAN",
        }
        assert columns == expected_columns

    def test_database_schema_wal_mode_enabled(self, tmp_path: Path) -> None:
        """
        Given an initialized database
        When we check the journal mode
        Then WAL (Write-Ahead Logging) is enabled for concurrent reads.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA journal_mode")
        journal_mode = cursor.fetchone()[0]
        conn.close()

        assert journal_mode.lower() == "wal"

    def test_database_schema_users_table_structure(self, tmp_path: Path) -> None:
        """
        Given an initialized database
        When we inspect users table
        Then it has correct columns for normalized user data.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "username": "TEXT",
            "password_hash": "TEXT",
            "role": "TEXT",
            "email": "TEXT",
            "created_at": "TEXT",
            "oidc_identity": "TEXT",
        }
        assert columns == expected_columns

    def test_database_schema_user_api_keys_foreign_key_cascade(
        self, tmp_path: Path
    ) -> None:
        """
        Given an initialized database with a user and api_key
        When we delete the user
        Then related api_keys are cascaded (deleted).
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Insert user
        conn.execute(
            """INSERT INTO users (username, password_hash, role, created_at)
               VALUES ('testuser', 'hash123', 'admin', '2024-01-01T00:00:00Z')"""
        )
        # Insert api key
        conn.execute(
            """INSERT INTO user_api_keys
               (key_id, username, key_hash, key_prefix, created_at)
               VALUES ('key1', 'testuser', 'keyhash', 'cidx_', '2024-01-01T00:00:00Z')"""
        )
        conn.commit()

        # Verify api key exists
        cursor = conn.execute(
            "SELECT COUNT(*) FROM user_api_keys WHERE username='testuser'"
        )
        assert cursor.fetchone()[0] == 1

        # Delete user
        conn.execute("DELETE FROM users WHERE username='testuser'")
        conn.commit()

        # Verify api key was cascaded
        cursor = conn.execute(
            "SELECT COUNT(*) FROM user_api_keys WHERE username='testuser'"
        )
        assert cursor.fetchone()[0] == 0

        conn.close()

    def test_database_schema_ssh_key_hosts_foreign_key_cascade(
        self, tmp_path: Path
    ) -> None:
        """
        Given an initialized database with an SSH key and host assignments
        When we delete the SSH key
        Then related host assignments are cascaded (deleted).
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Insert SSH key
        conn.execute(
            """INSERT INTO ssh_keys
               (name, fingerprint, key_type, private_path, public_path)
               VALUES ('mykey', 'fp123', 'ed25519', '/path/mykey', '/path/mykey.pub')"""
        )
        # Insert host assignment
        conn.execute(
            """INSERT INTO ssh_key_hosts (key_name, hostname)
               VALUES ('mykey', 'github.com')"""
        )
        conn.commit()

        # Verify host assignment exists
        cursor = conn.execute(
            "SELECT COUNT(*) FROM ssh_key_hosts WHERE key_name='mykey'"
        )
        assert cursor.fetchone()[0] == 1

        # Delete SSH key
        conn.execute("DELETE FROM ssh_keys WHERE name='mykey'")
        conn.commit()

        # Verify host assignment was cascaded
        cursor = conn.execute(
            "SELECT COUNT(*) FROM ssh_key_hosts WHERE key_name='mykey'"
        )
        assert cursor.fetchone()[0] == 0

        conn.close()


class TestDatabaseIndexMigration:
    """Tests for justified performance indexes and migration of unjustified indexes.

    Story #269: Remove Unjustified SQLite Performance Indexes and Fix MCP Credential
    Lookup Algorithm.

    7 justified indexes must exist after initialize_database():
    1. idx_background_jobs_status ON background_jobs(status)
    2. idx_background_jobs_status_created ON background_jobs(status, created_at DESC)
    3. idx_background_jobs_completed_status ON background_jobs(completed_at, status)
    4. idx_user_api_keys_username ON user_api_keys(username)
    5. idx_user_mcp_credentials_username ON user_mcp_credentials(username)
    6. idx_user_mcp_credentials_client_id ON user_mcp_credentials(client_id)
    7. idx_research_messages_session_id ON research_messages(session_id) [single-column]

    5 unjustified indexes that must NOT exist (or must be dropped if found):
    1. idx_background_jobs_operation_type
    2. idx_sync_jobs_username_status
    3. idx_sync_jobs_status
    4. idx_sync_jobs_created_at
    5. idx_user_api_keys_key_hash
    """

    JUSTIFIED_INDEX_NAMES = {
        "idx_background_jobs_status",
        "idx_background_jobs_status_created",
        "idx_background_jobs_completed_status",
        "idx_user_api_keys_username",
        "idx_user_mcp_credentials_username",
        "idx_user_mcp_credentials_client_id",
        "idx_research_messages_session_id",
    }

    UNJUSTIFIED_INDEX_NAMES = {
        "idx_background_jobs_operation_type",
        "idx_sync_jobs_username_status",
        "idx_sync_jobs_status",
        "idx_sync_jobs_created_at",
        "idx_user_api_keys_key_hash",
    }

    def _get_all_indexes(self, db_path) -> dict:
        """Helper: returns dict of {index_name: sql} for all user-created indexes."""
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
            return {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()

    def test_fresh_database_has_7_justified_indexes_and_no_unjustified(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 1: Justified indexes are created on fresh database.

        Given a fresh SQLite database with no tables
        When initialize_database() is called
        Then exactly 7 justified performance indexes exist
        And the 5 unjustified indexes do NOT exist.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test_fresh.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        all_indexes = self._get_all_indexes(db_path)

        # All 7 justified indexes must be present
        for idx_name in self.JUSTIFIED_INDEX_NAMES:
            assert idx_name in all_indexes, (
                f"Expected justified index '{idx_name}' not found. "
                f"Found: {sorted(all_indexes.keys())}"
            )

        # No unjustified indexes must exist
        for idx_name in self.UNJUSTIFIED_INDEX_NAMES:
            assert idx_name not in all_indexes, (
                f"Unjustified index '{idx_name}' must NOT exist on fresh database. "
                f"Found: {sorted(all_indexes.keys())}"
            )

    def test_migration_drops_unjustified_indexes_from_existing_database(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 2: Unjustified indexes are dropped from existing database.

        Given an existing database that has all 12 original indexes from commit 0d5af105
        When initialize_database() is called (server restart triggers migration)
        Then the 5 unjustified indexes are dropped
        And idx_user_mcp_credentials_client_id remains present.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test_migration.db"
        schema = DatabaseSchema(str(db_path))

        # First initialize to create all tables
        schema.initialize_database()

        # Now manually inject all 5 unjustified indexes (simulating existing
        # staging/production database state from commit 0d5af105)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_background_jobs_operation_type "
                "ON background_jobs(operation_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_jobs_username_status "
                "ON sync_jobs(username, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_jobs_status "
                "ON sync_jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_jobs_created_at "
                "ON sync_jobs(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_api_keys_key_hash "
                "ON user_api_keys(key_hash)"
            )
            conn.commit()
        finally:
            conn.close()

        # Verify unjustified indexes are present before migration
        indexes_before = self._get_all_indexes(db_path)
        assert "idx_background_jobs_operation_type" in indexes_before
        assert "idx_sync_jobs_status" in indexes_before

        # Run initialize_database again (as happens on server restart)
        schema.initialize_database()

        # Verify all unjustified indexes are gone
        indexes_after = self._get_all_indexes(db_path)
        for idx_name in self.UNJUSTIFIED_INDEX_NAMES:
            assert idx_name not in indexes_after, (
                f"Unjustified index '{idx_name}' should have been dropped by migration. "
                f"Still present in: {sorted(indexes_after.keys())}"
            )

        # Verify all 7 justified indexes are present
        for idx_name in self.JUSTIFIED_INDEX_NAMES:
            assert idx_name in indexes_after, (
                f"Justified index '{idx_name}' missing after migration. "
                f"Found: {sorted(indexes_after.keys())}"
            )

    def test_research_messages_index_is_single_column_not_composite(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 2 (detail): Old composite idx_research_messages_session_id is replaced
        by single-column ON (session_id).

        Given an existing database with old composite research_messages index
        When initialize_database() is called
        Then the index covers only session_id (single column) not session_id+created_at.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test_research_idx.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        # Check the SQL definition of the research_messages index
        all_indexes = self._get_all_indexes(db_path)
        assert "idx_research_messages_session_id" in all_indexes

        idx_sql = all_indexes["idx_research_messages_session_id"]
        # The SQL should reference only session_id, NOT created_at
        assert "session_id" in idx_sql, (
            f"Index SQL should contain session_id: {idx_sql}"
        )
        assert "created_at" not in idx_sql, (
            f"Index should be single-column (session_id only), "
            f"but found created_at in: {idx_sql}"
        )

    def test_index_migration_is_idempotent(self, tmp_path: Path) -> None:
        """
        Scenario 3: Migrations are idempotent.

        Given an initialized database with all justified indexes present
        When initialize_database() is called a second time
        Then no errors occur and the same 7 justified indexes exist.
        """
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test_idempotent.db"
        schema = DatabaseSchema(str(db_path))

        # First call
        schema.initialize_database()

        # Second call must not raise and must leave same indexes
        schema.initialize_database()

        all_indexes = self._get_all_indexes(db_path)

        # All 7 justified indexes must still be present
        for idx_name in self.JUSTIFIED_INDEX_NAMES:
            assert idx_name in all_indexes, (
                f"Justified index '{idx_name}' missing after second initialize_database(). "
                f"Found: {sorted(all_indexes.keys())}"
            )

        # No unjustified indexes must exist
        for idx_name in self.UNJUSTIFIED_INDEX_NAMES:
            assert idx_name not in all_indexes, (
                f"Unjustified index '{idx_name}' appeared after second initialize_database()."
            )


class TestDatabaseConnectionManager:
    """Tests for DatabaseConnectionManager with thread-local connection pooling."""

    def test_get_connection_returns_valid_connection(self, tmp_path: Path) -> None:
        """
        Given an initialized database
        When get_connection() is called
        Then it returns a valid SQLite connection.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
            DatabaseSchema,
        )

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        manager = DatabaseConnectionManager(str(db_path))
        conn = manager.get_connection()

        assert conn is not None
        # Verify connection works
        cursor = conn.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1

        manager.close_all()

    def test_get_connection_reuses_thread_local_connection(
        self, tmp_path: Path
    ) -> None:
        """
        Given an initialized database and a thread
        When get_connection() is called multiple times from same thread
        Then it returns the same connection object.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
            DatabaseSchema,
        )

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        manager = DatabaseConnectionManager(str(db_path))
        conn1 = manager.get_connection()
        conn2 = manager.get_connection()

        assert conn1 is conn2

        manager.close_all()

    def test_execute_atomic_commits_on_success(self, tmp_path: Path) -> None:
        """
        Given an initialized database
        When execute_atomic() succeeds
        Then changes are committed.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
            DatabaseSchema,
        )

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        manager = DatabaseConnectionManager(str(db_path))

        def operation(conn):
            conn.execute(
                """INSERT INTO users (username, password_hash, role, created_at)
                   VALUES ('testuser', 'hash', 'admin', '2024-01-01')"""
            )
            return True

        result = manager.execute_atomic(operation)
        assert result is True

        # Verify data persisted
        conn = manager.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM users WHERE username='testuser'")
        assert cursor.fetchone()[0] == 1

        manager.close_all()

    def test_execute_atomic_rolls_back_on_error(self, tmp_path: Path) -> None:
        """
        Given an initialized database with existing data
        When execute_atomic() raises an exception
        Then changes are rolled back.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
            DatabaseSchema,
        )

        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        manager = DatabaseConnectionManager(str(db_path))

        # Insert initial user
        def setup(conn):
            conn.execute(
                """INSERT INTO users (username, password_hash, role, created_at)
                   VALUES ('existinguser', 'hash', 'admin', '2024-01-01')"""
            )
            return True

        manager.execute_atomic(setup)

        # Try operation that will fail
        def failing_operation(conn):
            conn.execute(
                """INSERT INTO users (username, password_hash, role, created_at)
                   VALUES ('newuser', 'hash', 'admin', '2024-01-01')"""
            )
            raise RuntimeError("Simulated failure")

        with pytest.raises(RuntimeError, match="Simulated failure"):
            manager.execute_atomic(failing_operation)

        # Verify rollback occurred - newuser should not exist
        conn = manager.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM users WHERE username='newuser'")
        assert cursor.fetchone()[0] == 0

        # Verify existing data still exists
        cursor = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username='existinguser'"
        )
        assert cursor.fetchone()[0] == 1

        manager.close_all()
