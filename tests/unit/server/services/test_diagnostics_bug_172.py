"""
Unit tests for Bug #172: DiagnosticsService versioned path resolution.

Bug #172: The _validate_hnsw_indexes() method uses raw clone_path from the database
instead of resolving the actual filesystem path for versioned repositories. This causes
false positives for "Missing .code-indexer/index directory" even when indexes are healthy.

Tests verify:
- Flat-structure repos (no .versioned/) work correctly
- Versioned repos resolve to the latest v_* directory
- Mixed topology (some versioned, some flat) is handled correctly
- Edge case: .versioned/ dir exists but has no v_* subdirectories
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


class TestDiagnosticsBug172VersionedPathResolution:
    """Test HNSW index validation with versioned repository structure."""

    @pytest.mark.asyncio
    async def test_flat_structure_repos_work_correctly(self, tmp_path):
        """Test that flat-structure repos (no .versioned/) are validated correctly."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create flat-structure repo with valid HNSW index
        repo_dir = golden_repos_dir / "flat-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # Create valid HNSW index
        self._create_valid_hnsw_index(repo_index_dir, vector_count=10)

        # Register repo in database with flat path
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("flat-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should detect healthy index
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details["repos_with_issues"]) == 0

    @pytest.mark.asyncio
    async def test_versioned_repos_resolve_to_latest_version(self, tmp_path):
        """
        Test that versioned repos resolve to the latest v_* directory.

        This is the core Bug #172 scenario: database has flat path, but repo
        is stored in .versioned/{alias}/v_*/. Diagnostic should resolve to
        the actual versioned path, not the stale database path.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Database has stale flat path (doesn't exist on disk)
        stale_db_path = golden_repos_dir / "versioned-repo"

        # Actual repo is in versioned structure
        versioned_base = golden_repos_dir / ".versioned" / "versioned-repo"
        v1_dir = versioned_base / "v_1000000000"
        v2_dir = versioned_base / "v_2000000000"  # Latest
        v3_dir = versioned_base / "v_1500000000"  # Middle timestamp

        # Create indexes in all versions (latest should be checked)
        for version_dir in [v1_dir, v2_dir, v3_dir]:
            repo_index_dir = version_dir / ".code-indexer" / "index" / "voyage-code-3"
            repo_index_dir.mkdir(parents=True)
            # Only v2 (latest) has healthy index
            if version_dir == v2_dir:
                self._create_valid_hnsw_index(repo_index_dir, vector_count=10)
            else:
                # Older versions have corrupt/missing indexes
                self._create_collection_metadata(repo_index_dir, vector_count=5)

        # Register repo in database with STALE path (doesn't exist)
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("versioned-repo", str(stale_db_path))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # BUG #172 FIX: Should resolve to v2_dir (latest) and find healthy index
            # WITHOUT the fix, it would check stale_db_path and report "Missing .code-indexer/index directory"
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details["repos_with_issues"]) == 0

    @pytest.mark.asyncio
    async def test_mixed_topology_handles_both_flat_and_versioned(self, tmp_path):
        """Test that mixed topology (some flat, some versioned) is handled correctly."""
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Flat repo with healthy index
        flat_repo_dir = golden_repos_dir / "flat-repo"
        flat_index_dir = flat_repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        flat_index_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(flat_index_dir, vector_count=10)

        # Versioned repo with healthy index (stale DB path)
        stale_versioned_path = golden_repos_dir / "versioned-repo"
        versioned_base = golden_repos_dir / ".versioned" / "versioned-repo"
        v_latest = versioned_base / "v_9999999999"
        versioned_index_dir = v_latest / ".code-indexer" / "index" / "voyage-code-3"
        versioned_index_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(versioned_index_dir, vector_count=10)

        # Register both repos
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path,
            [
                ("flat-repo", str(flat_repo_dir)),
                ("versioned-repo", str(stale_versioned_path)),  # Stale path
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

            # Both repos should be healthy
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 2
            assert result.details["repos_with_healthy_indexes"] == 2
            assert len(result.details["repos_with_issues"]) == 0

    @pytest.mark.asyncio
    async def test_versioned_dir_exists_but_no_valid_versions(self, tmp_path):
        """
        Test edge case: .versioned/{alias}/ exists but has no valid v_* subdirectories.

        Should fall back to checking the database clone_path (which doesn't exist),
        and report missing index directory.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Database path (doesn't exist)
        db_path_value = golden_repos_dir / "edge-case-repo"

        # Versioned directory exists but is empty (no v_* subdirectories)
        versioned_base = golden_repos_dir / ".versioned" / "edge-case-repo"
        versioned_base.mkdir(parents=True)
        # Create some non-version directories (should be ignored)
        (versioned_base / "not-a-version").mkdir()
        (versioned_base / "random-file.txt").write_text("test")

        # Register repo
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("edge-case-repo", str(db_path_value))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should report missing index (falls back to non-existent DB path)
            assert result.status in [DiagnosticStatus.WARNING, DiagnosticStatus.ERROR]
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 0
            assert len(result.details["repos_with_issues"]) == 1
            assert any(
                "missing" in str(issue).lower() or "not found" in str(issue).lower()
                for issue in result.details["repos_with_issues"]
            )

    @pytest.mark.asyncio
    async def test_versioned_repo_with_malformed_version_directories(self, tmp_path):
        """
        Test that malformed version directories (not v_TIMESTAMP) are skipped gracefully.

        Only valid v_* directories with valid timestamps should be considered.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Database path (doesn't exist)
        stale_db_path = golden_repos_dir / "malformed-repo"

        # Versioned structure with mix of valid and malformed directories
        versioned_base = golden_repos_dir / ".versioned" / "malformed-repo"

        # Malformed version directories (should be ignored)
        (versioned_base / "v_").mkdir(parents=True)  # No timestamp
        (versioned_base / "v_abc").mkdir(parents=True)  # Non-numeric timestamp
        (versioned_base / "vno_underscore").mkdir(parents=True)  # Wrong format

        # Valid version directory with healthy index
        v_valid = versioned_base / "v_5555555555"
        valid_index_dir = v_valid / ".code-indexer" / "index" / "voyage-code-3"
        valid_index_dir.mkdir(parents=True)
        self._create_valid_hnsw_index(valid_index_dir, vector_count=10)

        # Register repo
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("malformed-repo", str(stale_db_path))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should find the valid version and report healthy
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details["repos_with_issues"]) == 0

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
