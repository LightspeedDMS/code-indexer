"""
Unit tests for Vector Storage HNSW Index Validation (Bug #147).

Tests comprehensive HNSW index validation across golden repositories:
- Scanning golden-repos directory structure
- Validating code semantic indexes (voyage-3)
- Validating temporal indexes (code-indexer-temporal)
- Detecting missing/corrupted HNSW indexes
- Per-repo status reporting with aggregates
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)


class TestVectorStorageHNSWValidation:
    """Test HNSW index validation in vector storage diagnostics."""

    @pytest.mark.asyncio
    async def test_check_vector_storage_validates_hnsw_indexes(self, tmp_path):
        """Test that vector storage diagnostic validates actual HNSW indexes."""
        # Create golden repos structure with HNSW indexes
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create repo1 with valid HNSW index
        repo1_dir = golden_repos_dir / "repo1"
        repo1_index_dir = repo1_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo1_index_dir.mkdir(parents=True)

        # Create valid HNSW index for repo1
        self._create_valid_hnsw_index(repo1_index_dir, vector_count=10)

        # Create repo2 with missing HNSW index
        repo2_dir = golden_repos_dir / "repo2"
        repo2_index_dir = repo2_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo2_index_dir.mkdir(parents=True)
        # Only create metadata, no hnsw_index.bin
        self._create_collection_metadata(repo2_index_dir, vector_count=5)

        # Bug #149 Fix: Register repos in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path,
            [
                ("repo1", str(repo1_dir)),
                ("repo2", str(repo2_dir)),
            ],
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should detect the problem
            assert result.status in [DiagnosticStatus.WARNING, DiagnosticStatus.ERROR]
            assert "hnsw" in result.message.lower() or "index" in result.message.lower()
            assert "details" in dir(result)

            # Should report per-repo status
            details = result.details
            assert "repos_checked" in details
            assert details["repos_checked"] == 2
            assert "repos_with_healthy_indexes" in details
            assert details["repos_with_healthy_indexes"] == 1  # Only repo1 healthy
            assert "repos_with_issues" in details
            assert len(details["repos_with_issues"]) == 1
            assert any("repo2" in str(issue) for issue in details["repos_with_issues"])

    @pytest.mark.asyncio
    async def test_check_vector_storage_detects_corrupted_hnsw_index(self, tmp_path):
        """Test that diagnostic detects corrupted HNSW indexes."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "corrupted-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # Create corrupted HNSW index (invalid binary data)
        hnsw_file = repo_index_dir / "hnsw_index.bin"
        hnsw_file.write_bytes(b"corrupted data that is not a valid HNSW index")

        # Create metadata claiming there's an index
        self._create_collection_metadata(repo_index_dir, vector_count=10)

        # Bug #149 Fix: Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("corrupted-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should detect corruption
            assert result.status == DiagnosticStatus.ERROR
            assert "repos_with_issues" in result.details
            assert len(result.details["repos_with_issues"]) == 1
            assert "corrupted" in str(result.details["repos_with_issues"][0]).lower() or \
                   "load" in str(result.details["repos_with_issues"][0]).lower()

    @pytest.mark.asyncio
    async def test_check_vector_storage_validates_multiple_index_types(self, tmp_path):
        """Test that diagnostic validates both code semantic and temporal indexes."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "multi-index-repo"
        repo_base = repo_dir / ".code-indexer" / "index"

        # Create code semantic index
        code_index_dir = repo_base / "voyage-code-3"
        code_index_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(code_index_dir, vector_count=20)

        # Create temporal index
        temporal_index_dir = repo_base / "code-indexer-temporal"
        temporal_index_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(temporal_index_dir, vector_count=100, vector_dim=1024)

        # Bug #149 Fix: Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("multi-index-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should detect both index types
            assert result.status == DiagnosticStatus.WORKING
            assert "index_types_found" in result.details
            index_types = result.details["index_types_found"]
            assert "voyage-code-3" in index_types or "code semantic" in str(index_types).lower()
            assert "code-indexer-temporal" in index_types or "temporal" in str(index_types).lower()

    @pytest.mark.asyncio
    async def test_check_vector_storage_with_no_golden_repos(self, tmp_path):
        """Test diagnostic with no golden repositories configured."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        # Empty directory - no repos

        # Bug #149 Fix: Create empty database (no registered repos)
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(db_path, [])

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should report no repos found
            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "no" in result.message.lower() and "repos" in result.message.lower()
            assert result.details["repos_checked"] == 0

    @pytest.mark.asyncio
    async def test_check_vector_storage_skips_versioned_directories(self, tmp_path):
        """Test that diagnostic skips .versioned directories."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create versioned directory (should be skipped - not registered)
        versioned_dir = golden_repos_dir / ".versioned" / "repo-v1" / ".code-indexer" / "index" / "voyage-code-3"
        versioned_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(versioned_dir, vector_count=10)

        # Create real golden repo
        real_repo_dir = golden_repos_dir / "real-repo"
        real_repo_index = real_repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        real_repo_index.mkdir(parents=True)
        self._create_valid_hnsw_index(real_repo_index, vector_count=10)

        # Bug #149 Fix: Only register real repo (not versioned)
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("real-repo", str(real_repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should only count real repo, not versioned
            assert result.details["repos_checked"] == 1
            assert result.status == DiagnosticStatus.WORKING

    # Helper methods

    def _create_valid_hnsw_index(
        self, collection_dir: Path, vector_count: int, vector_dim: int = 1024
    ) -> None:
        """Create a valid HNSW index for testing."""
        try:
            import hnswlib
        except ImportError:
            pytest.skip("hnswlib not available")

        # Create HNSW index
        index = hnswlib.Index(space="cosine", dim=vector_dim)
        index.init_index(max_elements=vector_count, M=16, ef_construction=200)

        # Add random vectors
        vectors = np.random.rand(vector_count, vector_dim).astype(np.float32)
        labels = np.arange(vector_count)
        index.add_items(vectors, labels)

        # Save index
        hnsw_file = collection_dir / "hnsw_index.bin"
        index.save_index(str(hnsw_file))

        # Create metadata
        self._create_collection_metadata(collection_dir, vector_count, vector_dim)

    def _create_collection_metadata(
        self, collection_dir: Path, vector_count: int, vector_dim: int = 1024
    ) -> None:
        """Create collection_meta.json file."""
        metadata = {
            "name": collection_dir.name,
            "vector_size": vector_dim,
            "created_at": "2026-02-05T00:00:00.000000",
            "quantization_range": {"min": -0.75, "max": 0.75},
            "hnsw_index": {
                "version": 1,
                "vector_count": vector_count,
                "vector_dim": vector_dim,
                "M": 16,
                "ef_construction": 200,
                "space": "cosine",
                "last_rebuild": "2026-02-05T00:00:00.000000+00:00",
                "id_mapping": {str(i): f"id_{i}" for i in range(vector_count)},
            },
        }

        meta_file = collection_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

    def _create_database_with_registered_repos(
        self, db_path: Path, repos: list[tuple[str, str]]
    ):
        """
        Create SQLite database with registered golden repos.

        Args:
            db_path: Path to database file
            repos: List of (alias, clone_path) tuples
        """
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            # Create golden_repos_metadata table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                    alias TEXT PRIMARY KEY NOT NULL,
                    repo_url TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    clone_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enable_temporal INTEGER NOT NULL DEFAULT 0,
                    temporal_options TEXT
                )
                """
            )

            # Insert registered repos
            for alias, clone_path in repos:
                conn.execute(
                    """
                    INSERT INTO golden_repos_metadata
                    (alias, repo_url, default_branch, clone_path, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        alias,
                        f"git@github.com:test/{alias}.git",
                        "main",
                        clone_path,
                        "2025-01-01T00:00:00Z",
                    ),
                )

            conn.commit()
        finally:
            conn.close()
