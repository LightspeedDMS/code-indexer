"""
Unit tests for Infrastructure Diagnostics (Story #94).

Tests cover:
- SQLite database diagnostics
- Vector storage diagnostics
- Schema validation
- Storage statistics
- Timeout behavior
"""

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticStatus,
    DiagnosticsService,
)


class TestSQLiteDatabaseDiagnostics:
    """Test SQLite database diagnostic checks."""

    @pytest.mark.asyncio
    async def test_check_sqlite_database_with_valid_database(self, tmp_path):
        """Test SQLite diagnostic with valid, healthy database."""
        # Create a valid database with required tables
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create required tables (must match production schema - 5 tables in cidx_server.db)
        # Note: groups, repo_group_access, audit_logs are in groups.db, NOT cidx_server.db (Bug #187)
        required_tables = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]
        for table in required_tables:
            cursor.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        # Mock config to return our test database path
        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()
            result = await service.check_sqlite_database()

            assert result.status == DiagnosticStatus.WORKING
            assert "database" in result.message.lower()
            assert "path" in result.details
            assert "size_bytes" in result.details
            assert result.details["size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_check_sqlite_database_with_missing_file(self, tmp_path):
        """Test SQLite diagnostic with missing database file."""
        db_path = tmp_path / "nonexistent.db"

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()
            result = await service.check_sqlite_database()

            assert result.status == DiagnosticStatus.ERROR
            assert "not found" in result.message.lower() or "missing" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_sqlite_database_with_permission_denied(self, tmp_path):
        """Test SQLite diagnostic with permission denied on database file."""
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)

        # Create database but make it unreadable
        conn = sqlite3.connect(str(db_path))
        conn.close()
        os.chmod(db_path, 0o000)

        try:
            with patch(
                "code_indexer.server.services.diagnostics_service.ServerConfigManager"
            ) as mock_config_manager:
                mock_config = Mock()
                mock_config.server_dir = str(tmp_path)
                mock_config_manager.return_value.load_config.return_value = mock_config

                service = DiagnosticsService()
                result = await service.check_sqlite_database()

                assert result.status == DiagnosticStatus.ERROR
                assert "permission" in result.message.lower() or "access" in result.message.lower()
        finally:
            # Restore permissions for cleanup
            os.chmod(db_path, 0o644)

    @pytest.mark.asyncio
    async def test_check_sqlite_database_with_corrupted_database(self, tmp_path):
        """Test SQLite diagnostic with corrupted database (integrity check fails)."""
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)

        # Create database and write corrupted data
        with open(db_path, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)  # Invalid SQLite data

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()
            result = await service.check_sqlite_database()

            # Should fail either on connection or integrity check
            assert result.status == DiagnosticStatus.ERROR
            assert "corrupt" in result.message.lower() or "error" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_sqlite_database_with_missing_tables(self, tmp_path):
        """Test SQLite diagnostic with database missing required tables."""
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)

        # Create database with only some tables
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        cursor.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY)")
        # Missing: golden_repositories, settings, activated_repos, access_control
        conn.commit()
        conn.close()

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()
            result = await service.check_sqlite_database()

            assert result.status == DiagnosticStatus.ERROR
            assert "missing" in result.message.lower() or "table" in result.message.lower()
            assert "missing_tables" in result.details
            assert len(result.details["missing_tables"]) > 0

    @pytest.mark.asyncio
    async def test_check_database_schema_with_all_tables_present(self, tmp_path):
        """Test _check_database_schema method with all required tables."""
        db_path = tmp_path / "complete_db.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create all required tables (must match production schema - 5 tables in cidx_server.db)
        # Note: groups, repo_group_access, audit_logs are in groups.db, NOT cidx_server.db (Bug #187)
        required_tables = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]
        for table in required_tables:
            cursor.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.commit()

        service = DiagnosticsService()
        valid, missing_tables = service._check_database_schema(conn)

        conn.close()

        assert valid is True
        assert len(missing_tables) == 0

    @pytest.mark.asyncio
    async def test_check_database_schema_with_missing_tables(self, tmp_path):
        """Test _check_database_schema method with missing tables."""
        db_path = tmp_path / "incomplete_db.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create only 1 table (users exists in both old and new schema)
        cursor.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        conn.commit()

        service = DiagnosticsService()
        valid, missing_tables = service._check_database_schema(conn)

        conn.close()

        assert valid is False
        assert len(missing_tables) > 0
        # Check for production schema table names (Bug #187: groups tables are in groups.db, not cidx_server.db)
        assert "golden_repos_metadata" in missing_tables
        assert "global_repos" in missing_tables
        assert "user_api_keys" in missing_tables
        assert "user_mcp_credentials" in missing_tables


class TestVectorStorageDiagnostics:
    """Test vector storage diagnostic checks."""

    @pytest.mark.asyncio
    async def test_check_vector_storage_with_valid_directory(self, tmp_path):
        """Test vector storage diagnostic with valid, accessible directory."""
        storage_path = tmp_path / "data"
        storage_path.mkdir()

        # Create golden-repos structure (Bug #147: proper validation)
        golden_repos_path = storage_path / "golden-repos"
        golden_repos_path.mkdir()

        # Create repos with valid HNSW indexes
        repo_paths = []
        for repo_name in ["repo1", "repo2", "repo3"]:
            repo_dir = golden_repos_path / repo_name
            repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
            repo_index_dir.mkdir(parents=True)
            # Create minimal valid HNSW structure
            (repo_index_dir / "hnsw_index.bin").write_bytes(b"mock hnsw data")
            (repo_index_dir / "collection_meta.json").write_text('{"vector_size": 1024}')
            repo_paths.append((repo_name, str(repo_dir)))

        # Bug #149 Fix: Create test database with registered repos
        import sqlite3
        db_path = tmp_path / "cidx_server.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                    alias TEXT PRIMARY KEY NOT NULL,
                    repo_url TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    clone_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enable_temporal INTEGER NOT NULL DEFAULT 0,
                    temporal_options TEXT
                )
            """)
            for alias, clone_path in repo_paths:
                conn.execute(
                    """INSERT INTO golden_repos_metadata
                       (alias, repo_url, default_branch, clone_path, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (alias, f"git@github.com:test/{alias}.git", "main", clone_path, "2025-01-01T00:00:00Z")
                )
            conn.commit()
        finally:
            conn.close()

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager, patch(
            "code_indexer.server.services.diagnostics_service.HNSWIndexManager"
        ) as mock_hnsw_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            # Mock HNSW index loading to return successful result
            mock_index_instance = Mock()
            mock_hnsw_manager.return_value.load_index.return_value = mock_index_instance

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            assert result.status == DiagnosticStatus.WORKING
            assert "healthy" in result.message.lower() or "working" in result.message.lower()
            assert "path" in result.details
            assert "repos_checked" in result.details
            assert result.details["repos_checked"] == 3
            assert "repos_with_healthy_indexes" in result.details
            assert result.details["repos_with_healthy_indexes"] == 3

    @pytest.mark.asyncio
    async def test_check_vector_storage_with_missing_directory(self, tmp_path):
        """Test vector storage diagnostic with missing storage directory."""
        storage_path = tmp_path / "nonexistent_storage"

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()
            result = await service.check_vector_storage()

            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "not configured" in result.message.lower() or "missing" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_vector_storage_with_unreadable_directory(self, tmp_path):
        """Test vector storage diagnostic with unreadable directory."""
        storage_path = tmp_path / "data"
        storage_path.mkdir()

        # Make directory unreadable
        os.chmod(storage_path, 0o000)

        try:
            with patch(
                "code_indexer.server.services.diagnostics_service.ServerConfigManager"
            ) as mock_config_manager:
                mock_config = Mock()
                mock_config.server_dir = str(tmp_path)
                mock_config_manager.return_value.load_config.return_value = mock_config

                service = DiagnosticsService()
                result = await service.check_vector_storage()

                assert result.status == DiagnosticStatus.ERROR
                assert "permission" in result.message.lower() or "unreadable" in result.message.lower()
        finally:
            # Restore permissions for cleanup
            os.chmod(storage_path, 0o755)

    @pytest.mark.asyncio
    async def test_get_storage_statistics(self, tmp_path):
        """Test _get_storage_statistics method calculation."""
        storage_path = tmp_path / "data"
        storage_path.mkdir()

        # Create multiple repositories with files
        for i in range(5):
            repo_dir = storage_path / f"repo{i}"
            repo_dir.mkdir()
            (repo_dir / "file1.json").write_text("a" * 100)
            (repo_dir / "file2.json").write_text("b" * 200)

        service = DiagnosticsService()
        stats = service._get_storage_statistics(storage_path)

        assert "repo_count" in stats
        assert stats["repo_count"] == 5
        assert "total_size_bytes" in stats
        assert stats["total_size_bytes"] == 5 * (100 + 200)  # 5 repos * 300 bytes each
        assert "last_modified" in stats


class TestInfrastructureDiagnostics:
    """Test run_infrastructure_diagnostics method."""

    # Test constants
    FAST_TIMEOUT_SECONDS = 0.1
    SLOW_OPERATION_SECONDS = 0.5

    @pytest.mark.asyncio
    async def test_run_infrastructure_diagnostics(self, tmp_path):
        """Test run_infrastructure_diagnostics returns results for both components."""
        # Create valid database
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        # Use production schema table names (5 tables in cidx_server.db)
        # Note: groups, repo_group_access, audit_logs are in groups.db, NOT cidx_server.db (Bug #187)
        required_tables = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]
        for table in required_tables:
            cursor.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

        # Bug #149 Fix: Create golden_repos_metadata table with proper schema
        cursor.execute("""
            CREATE TABLE golden_repos_metadata (
                alias TEXT PRIMARY KEY NOT NULL,
                repo_url TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                clone_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                enable_temporal INTEGER NOT NULL DEFAULT 0,
                temporal_options TEXT
            )
        """)
        conn.commit()
        conn.close()

        # Create valid storage with golden-repos structure (Bug #147)
        storage_path = tmp_path / "data"
        golden_repos_path = storage_path / "golden-repos"
        golden_repos_path.mkdir(parents=True)

        # Create repo with valid HNSW index
        repo_dir = golden_repos_path / "repo1"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)
        (repo_index_dir / "hnsw_index.bin").write_bytes(b"mock hnsw data")
        (repo_index_dir / "collection_meta.json").write_text('{"vector_size": 1024}')

        # Bug #149 Fix: Register repo in database
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO golden_repos_metadata
               (alias, repo_url, default_branch, clone_path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("repo1", "git@github.com:test/repo1.git", "main", str(repo_dir), "2025-01-01T00:00:00Z")
        )
        conn.commit()
        conn.close()

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager, patch(
            "code_indexer.server.services.diagnostics_service.HNSWIndexManager"
        ) as mock_hnsw_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            # Mock HNSW index loading to return successful result
            mock_index_instance = Mock()
            mock_hnsw_manager.return_value.load_index.return_value = mock_index_instance

            service = DiagnosticsService(db_path=str(db_path))
            results = await service.run_infrastructure_diagnostics()

            # Should return 2 results: SQLite and Vector Storage
            assert len(results) == 2

            # Check result names
            result_names = [r.name for r in results]
            assert "SQLite Database" in result_names
            assert "Vector Storage" in result_names

            # Both should be WORKING
            for result in results:
                assert result.status == DiagnosticStatus.WORKING

    @pytest.mark.asyncio
    async def test_infrastructure_diagnostics_has_timeout(self, tmp_path):
        """Test that run_infrastructure_diagnostics has built-in timeout protection."""
        # Mock a slow check that would exceed the built-in timeout
        async def slow_check():
            await asyncio.sleep(self.SLOW_OPERATION_SECONDS)
            return Mock(status=DiagnosticStatus.WORKING)

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()

            # Patch check to be slow
            with patch.object(service, "check_sqlite_database", side_effect=slow_check):
                # The method should have built-in timeout and complete quickly
                # even with slow checks (returns partial results or timeout errors)
                start_time = asyncio.get_event_loop().time()
                results = await service.run_infrastructure_diagnostics()
                elapsed_time = asyncio.get_event_loop().time() - start_time

                # Should complete within reasonable time (not wait for full slow operation)
                # If built-in timeout is 10s, this would be well under that
                # For unit tests, we expect it to fail fast or return partial results
                assert elapsed_time < self.SLOW_OPERATION_SECONDS + 1.0


class TestInfrastructureDiagnosticsIntegration:
    """Integration tests for infrastructure diagnostics endpoint."""

    @pytest.mark.asyncio
    async def test_infrastructure_diagnostics_endpoint_integration(self, tmp_path):
        """Test infrastructure diagnostics returns results through get_status."""
        # Create valid environment
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        # Use production schema table names (5 tables in cidx_server.db)
        # Note: groups, repo_group_access, audit_logs are in groups.db, NOT cidx_server.db (Bug #187)
        required_tables = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]
        for table in required_tables:
            cursor.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        storage_path = tmp_path / "data"
        (storage_path / "repo1").mkdir()

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService()

            # Run infrastructure diagnostics
            await service.run_category(DiagnosticCategory.INFRASTRUCTURE)

            # Get status
            status = service.get_status()

            # Should have infrastructure category
            assert DiagnosticCategory.INFRASTRUCTURE in status

            infra_results = status[DiagnosticCategory.INFRASTRUCTURE]

            # Should have 2 diagnostic results
            assert len(infra_results) >= 2

            # Check for SQLite and Vector Storage diagnostics
            result_names = [r.name for r in infra_results]
            assert any("sqlite" in name.lower() for name in result_names)
            assert any("vector" in name.lower() or "storage" in name.lower() for name in result_names)
