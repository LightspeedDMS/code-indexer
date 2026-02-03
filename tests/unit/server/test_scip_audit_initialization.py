"""
Tests for SCIP Audit Database Eager Initialization (Story #19).

TDD: Tests written FIRST before implementation.
All tests use real components following MESSI Rule #1: No mocks.

This story fixes the issue where fresh installs show SCIP Audit database
as error (RED status) on the dashboard because scip_audit.db is only
created lazily when MCP handlers are first imported.

Solution: Add eager initialization in app.py following the pattern of
groups.db and oauth.db initialization.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest


class TestScipAuditDatabaseInitialization:
    """Tests for eager SCIP audit database initialization at server startup."""

    @pytest.fixture
    def temp_server_dir(self) -> Generator[Path, None, None]:
        """Create temporary server directory for testing."""
        with tempfile.TemporaryDirectory(prefix="cidx_scip_init_test_") as tmp:
            yield Path(tmp)

    def test_initialize_scip_audit_database_creates_file(self, temp_server_dir: Path):
        """
        AC1: scip_audit.db should be created in server data directory.

        Given a fresh server installation with no existing databases
        When initialize_scip_audit_database() is called
        Then scip_audit.db should be created in the specified directory
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        scip_audit_path = initialize_scip_audit_database(str(temp_server_dir))

        assert scip_audit_path.exists(), "scip_audit.db should be created"
        assert scip_audit_path.name == "scip_audit.db"
        assert scip_audit_path.parent == temp_server_dir

    def test_initialize_scip_audit_database_creates_table(self, temp_server_dir: Path):
        """
        AC2: scip_audit.db should have the correct schema.

        Given initialize_scip_audit_database() is called
        When the database is examined
        Then it should have the scip_dependency_installations table
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        scip_audit_path = initialize_scip_audit_database(str(temp_server_dir))

        with sqlite3.connect(str(scip_audit_path)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='scip_dependency_installations'"
            )
            result = cursor.fetchone()
            assert (
                result is not None
            ), "scip_dependency_installations table should exist"
            assert result[0] == "scip_dependency_installations"

    def test_initialize_scip_audit_database_creates_indexes(
        self, temp_server_dir: Path
    ):
        """
        AC2: scip_audit.db should have required indexes for efficient querying.

        Given initialize_scip_audit_database() is called
        When the database is examined
        Then it should have all required indexes
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        scip_audit_path = initialize_scip_audit_database(str(temp_server_dir))

        with sqlite3.connect(str(scip_audit_path)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='scip_dependency_installations'"
            )
            indexes = {row[0] for row in cursor.fetchall()}

            # Verify all required indexes exist
            required_indexes = {
                "idx_timestamp",
                "idx_repo_alias",
                "idx_job_id",
                "idx_project_language",
            }
            missing = required_indexes - indexes
            assert not missing, f"Missing indexes: {missing}"

    def test_initialize_scip_audit_database_is_idempotent(self, temp_server_dir: Path):
        """
        AC3: Initialization should be idempotent - running twice is safe.

        Given scip_audit.db already exists with data
        When initialize_scip_audit_database() is called again
        Then existing data should not be modified or lost
        And no errors should be raised
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        # First initialization
        scip_audit_path = initialize_scip_audit_database(str(temp_server_dir))

        # Insert test data
        with sqlite3.connect(str(scip_audit_path)) as conn:
            conn.execute(
                """
                INSERT INTO scip_dependency_installations
                (job_id, repo_alias, package, command)
                VALUES ('test-job', 'test-repo', 'test-pkg', 'test-cmd')
                """
            )
            conn.commit()

        # Verify data exists
        with sqlite3.connect(str(scip_audit_path)) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM scip_dependency_installations")
            count_before = cursor.fetchone()[0]
            assert count_before == 1

        # Second initialization (should be idempotent)
        scip_audit_path_2 = initialize_scip_audit_database(str(temp_server_dir))

        # Verify path is the same
        assert scip_audit_path == scip_audit_path_2

        # Verify data is preserved
        with sqlite3.connect(str(scip_audit_path)) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM scip_dependency_installations")
            count_after = cursor.fetchone()[0]
            assert count_after == 1, "Existing data should be preserved"

    def test_initialize_scip_audit_database_creates_parent_directories(
        self, temp_server_dir: Path
    ):
        """
        AC4: Initialization should create parent directories if needed.

        Given a server data directory that doesn't exist
        When initialize_scip_audit_database() is called
        Then parent directories should be created
        And scip_audit.db should be created successfully
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        # Use a nested path that doesn't exist
        nested_path = temp_server_dir / "nested" / "server" / "data"
        assert not nested_path.exists()

        scip_audit_path = initialize_scip_audit_database(str(nested_path))

        assert nested_path.exists(), "Parent directories should be created"
        assert scip_audit_path.exists(), "scip_audit.db should be created"

    def test_initialize_scip_audit_database_logs_warning_on_failure(
        self, temp_server_dir: Path, caplog
    ):
        """
        AC5: Initialization failure should log warning, not block startup.

        Given a scenario where database creation might fail
        When initialize_scip_audit_database() encounters an error
        Then it should log a warning
        And return None instead of raising an exception
        """
        import logging
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )

        # Create a file where we expect to create the DB (simulates permission error)
        # Actually, let's test the permission error scenario differently
        # by making the directory read-only
        read_only_dir = temp_server_dir / "readonly"
        read_only_dir.mkdir()

        # Create a file with the same name as expected database
        # This should cause an error when trying to create table
        bad_db_path = read_only_dir / "scip_audit.db"
        bad_db_path.write_text("not a valid sqlite database")

        with caplog.at_level(logging.WARNING):
            result = initialize_scip_audit_database(str(read_only_dir))

        # Should return None on failure
        assert result is None, "Should return None on initialization failure"
        # Should log a warning
        assert any(
            "scip_audit" in record.message.lower() or "failed" in record.message.lower()
            for record in caplog.records
        ), "Should log warning about initialization failure"


class TestScipAuditDatabaseSchema:
    """Tests for SCIP audit database schema compatibility."""

    @pytest.fixture
    def temp_server_dir(self) -> Generator[Path, None, None]:
        """Create temporary server directory for testing."""
        with tempfile.TemporaryDirectory(prefix="cidx_schema_test_") as tmp:
            yield Path(tmp)

    def test_schema_matches_scip_audit_repository(self, temp_server_dir: Path):
        """
        Schema created by initialize_scip_audit_database should match
        the schema expected by SCIPAuditRepository.

        This ensures compatibility between eager initialization and
        the repository class used by MCP handlers.
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )
        from code_indexer.server.repositories.scip_audit import SCIPAuditRepository

        # Initialize via startup function
        scip_audit_path = initialize_scip_audit_database(str(temp_server_dir))

        # Use repository to insert and query (verifies schema compatibility)
        repo = SCIPAuditRepository(db_path=str(scip_audit_path))

        # Insert a record
        record_id = repo.create_audit_record(
            job_id="test-job-123",
            repo_alias="test-repo",
            package="test-package",
            command="pip install test-package",
            project_path="src/test",
            project_language="python",
            project_build_system="pip",
            reasoning="Test reasoning",
            username="testuser",
        )

        assert record_id is not None, "Should be able to insert record"
        assert record_id > 0

        # Query the record back
        records, total = repo.query_audit_records(job_id="test-job-123")
        assert total == 1, "Should find the inserted record"
        assert records[0]["repo_alias"] == "test-repo"


class TestHealthServiceWithEagerInitialization:
    """Tests for database health service with eager SCIP audit initialization."""

    @pytest.fixture
    def temp_server_dir_with_all_dbs(self) -> Generator[Path, None, None]:
        """Create temporary server directory with all 7 databases."""
        with tempfile.TemporaryDirectory(prefix="cidx_health_test_") as tmp:
            server_dir = Path(tmp)
            data_dir = server_dir / "data"
            data_dir.mkdir(parents=True)

            # Create payload_cache.db in correct location: data/golden-repos/.cache/
            cache_dir = data_dir / "golden-repos" / ".cache"
            cache_dir.mkdir(parents=True)

            # Create all 7 central database files with proper schema
            # scip_audit.db will be created via initialize_scip_audit_database
            # Note: payload_cache.db goes in data/golden-repos/.cache/, not server root
            databases = {
                "cidx_server.db": "CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)",
                "oauth.db": "CREATE TABLE IF NOT EXISTS oauth_providers (id INTEGER PRIMARY KEY)",
                "refresh_tokens.db": "CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY)",
                "logs.db": "CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY)",
                "groups.db": "CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY)",
            }

            for db_name, schema in databases.items():
                if db_name == "cidx_server.db":
                    db_path = data_dir / db_name
                else:
                    db_path = server_dir / db_name
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute(schema)
                    conn.commit()

            # Create payload_cache.db in the correct location
            payload_cache_path = cache_dir / "payload_cache.db"
            with sqlite3.connect(str(payload_cache_path)) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS cache (id INTEGER PRIMARY KEY)"
                )
                conn.commit()

            yield server_dir

    def test_scip_audit_healthy_after_eager_initialization(
        self, temp_server_dir_with_all_dbs: Path
    ):
        """
        AC6: After eager initialization, SCIP Audit should show healthy status.

        Given a fresh server installation
        When scip_audit.db is eagerly initialized at startup
        And the database health service checks all databases
        Then SCIP Audit should have HEALTHY status (green)
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthStatus,
        )

        # Eagerly initialize scip_audit.db
        initialize_scip_audit_database(str(temp_server_dir_with_all_dbs))

        # Check health
        service = DatabaseHealthService(server_dir=str(temp_server_dir_with_all_dbs))
        health_results = service.get_all_database_health()

        # Find SCIP Audit result
        scip_audit_result = next(
            (r for r in health_results if r.file_name == "scip_audit.db"), None
        )

        assert scip_audit_result is not None, "Should have SCIP Audit health result"
        assert scip_audit_result.status == DatabaseHealthStatus.HEALTHY, (
            f"SCIP Audit should be healthy, got {scip_audit_result.status}. "
            f"Checks: {scip_audit_result.checks}"
        )

    def test_all_7_databases_healthy_after_fresh_install(
        self, temp_server_dir_with_all_dbs: Path
    ):
        """
        AC7: All 7 databases should show healthy status on fresh install.

        Given a fresh CIDX server installation with no existing databases
        When the server starts up for the first time
        Then all 7 databases should show healthy status without user intervention
        """
        from code_indexer.server.startup.database_init import (
            initialize_scip_audit_database,
        )
        from code_indexer.server.services.database_health_service import (
            DatabaseHealthService,
            DatabaseHealthStatus,
        )

        # Eagerly initialize scip_audit.db (simulates server startup)
        initialize_scip_audit_database(str(temp_server_dir_with_all_dbs))

        # Check health of all databases
        service = DatabaseHealthService(server_dir=str(temp_server_dir_with_all_dbs))
        health_results = service.get_all_database_health()

        # Verify all 7 databases are healthy
        assert (
            len(health_results) == 7
        ), f"Expected 7 databases, got {len(health_results)}"

        for result in health_results:
            assert result.status == DatabaseHealthStatus.HEALTHY, (
                f"{result.display_name} ({result.file_name}) should be healthy, "
                f"got {result.status}. Checks: {result.checks}"
            )
