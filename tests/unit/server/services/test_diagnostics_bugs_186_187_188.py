"""
Unit tests for Bug #186, #187, #188 diagnostic fixes.

Bug #186: Claude Server/Delegation NoneType crash when config file doesn't exist
Bug #187: SQLite wrong-DB table check (groups tables in wrong database)
Bug #188: Vector Storage false positive on temporal collections
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)


class TestBug186ClaudeDelegationNoneType:
    """Test Bug #186: NoneType crash when delegation config doesn't exist."""

    @pytest.mark.asyncio
    async def test_check_claude_server_with_missing_config_file(self, tmp_path):
        """
        Test that check_claude_server() handles None from load_config() gracefully.

        Before fix: AttributeError: 'NoneType' object has no attribute 'is_configured'
        After fix: Returns NOT_CONFIGURED status without crash
        """
        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = Mock()
            # Simulate load_config() returning None when file doesn't exist
            mock_manager.load_config.return_value = None
            mock_manager_class.return_value = mock_manager

            service = DiagnosticsService()
            result = await service.check_claude_server()

            # Should not crash and return NOT_CONFIGURED
            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_claude_delegation_credentials_with_missing_config_file(self, tmp_path):
        """
        Test that check_claude_delegation_credentials() handles None from load_config() gracefully.

        Before fix: AttributeError: 'NoneType' object has no attribute 'is_configured'
        After fix: Returns NOT_CONFIGURED status without crash
        """
        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = Mock()
            # Simulate load_config() returning None when file doesn't exist
            mock_manager.load_config.return_value = None
            mock_manager_class.return_value = mock_manager

            service = DiagnosticsService()
            result = await service.check_claude_delegation_credentials()

            # Should not crash and return NOT_CONFIGURED
            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "not configured" in result.message.lower()


class TestBug187SQLiteWrongDBTableCheck:
    """Test Bug #187: SQLite checking for tables that live in groups.db, not cidx_server.db."""

    @pytest.mark.asyncio
    async def test_check_database_schema_without_groups_tables(self, tmp_path):
        """
        Test that database schema check doesn't require groups/repo_group_access/audit_logs tables.

        Before fix: Schema check fails because it looks for groups tables in cidx_server.db
        After fix: Schema check passes without groups tables (they're in groups.db)
        """
        # Create a valid database WITHOUT groups tables
        db_path = tmp_path / "data" / "cidx_server.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create only the tables that should be in cidx_server.db
        # (NOT groups, repo_group_access, audit_logs - those are in groups.db)
        tables_for_cidx_server_db = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]
        for table in tables_for_cidx_server_db:
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

            # Should pass without requiring groups tables
            assert result.status == DiagnosticStatus.WORKING
            assert "healthy" in result.message.lower()
            assert result.details.get("schema_valid") is True


class TestBug188VectorStorageTemporalCollections:
    """Test Bug #188: Vector Storage false positive on temporal collections."""

    @pytest.mark.asyncio
    async def test_check_collection_health_with_temporal_collection(self, tmp_path):
        """
        Test that temporal collections are validated correctly (no hnsw_index.bin required).

        Before fix: Returns error "Missing HNSW index file" for temporal collections
        After fix: Validates FilesystemVectorStore format (projection_matrix.npy, collection_meta.json)
        """
        # Create a temporal collection directory with FilesystemVectorStore format
        collection_dir = tmp_path / "code-indexer-temporal"
        collection_dir.mkdir(parents=True)

        # Create temporal collection files
        (collection_dir / "temporal_metadata.db").touch()
        (collection_dir / "temporal_progress.json").write_text("{}")
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"vector_size": 1024, "collection_name": "code-indexer-temporal"})
        )
        (collection_dir / "projection_matrix.npy").touch()

        # Create some quantized hex directories (typical for FilesystemVectorStore)
        for hex_dir in ["49", "55", "a6"]:
            (collection_dir / hex_dir).mkdir()

        service = DiagnosticsService()
        result = service._check_collection_health(
            collection_dir, "test-repo", "code-indexer-temporal"
        )

        # Should return None (healthy) because temporal collection has correct format
        assert result is None

    @pytest.mark.asyncio
    async def test_check_collection_health_with_hnsw_collection(self, tmp_path):
        """
        Test that HNSW collections are still validated correctly.

        This ensures the fix doesn't break validation of non-temporal collections.
        """
        # Create an HNSW collection directory
        collection_dir = tmp_path / "voyage-code-3"
        collection_dir.mkdir(parents=True)

        # Create HNSW collection files (no temporal_metadata.db)
        (collection_dir / "hnsw_index.bin").touch()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({
                "vector_size": 1024,
                "hnsw_index": {"vector_dim": 1024}
            })
        )

        # Mock HNSWIndexManager to avoid actually loading the index
        with patch(
            "code_indexer.server.services.diagnostics_service.HNSWIndexManager"
        ) as mock_hnsw_class:
            mock_manager = Mock()
            mock_manager.load_index.return_value = Mock()  # Simulate successful load
            mock_hnsw_class.return_value = mock_manager

            service = DiagnosticsService()
            result = service._check_collection_health(
                collection_dir, "test-repo", "voyage-code-3"
            )

            # Should return None (healthy)
            assert result is None

    @pytest.mark.asyncio
    async def test_check_collection_health_with_missing_temporal_files(self, tmp_path):
        """
        Test that temporal collections with missing required files are detected as unhealthy.
        """
        # Create a temporal collection directory (detected by temporal_metadata.db)
        collection_dir = tmp_path / "code-indexer-temporal"
        collection_dir.mkdir(parents=True)

        # Only create temporal_metadata.db, missing projection_matrix.npy and collection_meta.json
        (collection_dir / "temporal_metadata.db").touch()

        service = DiagnosticsService()
        result = service._check_collection_health(
            collection_dir, "test-repo", "code-indexer-temporal"
        )

        # Should detect missing files
        assert result is not None
        assert result["repo"] == "test-repo"
        assert "missing" in result["issue"].lower() or "projection_matrix" in result["issue"].lower()
