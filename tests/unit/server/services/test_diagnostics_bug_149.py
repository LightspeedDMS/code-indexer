"""
Unit tests for Bug #149: Vector Storage Diagnostic Scans Wrong Directories.

Verifies that the vector storage diagnostic ONLY scans registered golden repos
from the database, not random filesystem directories.

Bug #149 Issue:
    The diagnostic was scanning EVERY folder in /data/ (aliases, golden-repos container, cidx-meta)
    instead of only registered repositories.

Fix:
    Query golden_repos_metadata table to get list of registered repos,
    then ONLY scan those specific repo directories.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch
import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)


class TestBug149VectorStorageScanCorrectDirectories:
    """Test that vector storage diagnostic scans ONLY registered repos."""

    @pytest.mark.asyncio
    async def test_vector_storage_ignores_non_repo_directories(self, tmp_path):
        """
        Test that diagnostic ONLY scans registered golden repos, not random directories.

        Bug #149: The diagnostic was scanning random folders like:
        - aliases/ (not a repo)
        - golden-repos/ (the container folder itself, not repos inside)
        - cidx-meta/ (random folder)

        Fix: Query database for registered repos, scan ONLY those.
        """
        # Create golden-repos structure with mixed content
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create RANDOM directories that should be IGNORED
        (golden_repos_dir / "aliases").mkdir()  # Not a repo
        (golden_repos_dir / "cidx-meta").mkdir()  # Not a repo
        (golden_repos_dir / ".versioned").mkdir()  # Not a repo

        # Create actual repo directories
        repo1_dir = golden_repos_dir / "python-mock"
        repo1_index = repo1_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo1_index.mkdir(parents=True)
        self._create_valid_hnsw_index(repo1_index, vector_count=5)

        repo2_dir = golden_repos_dir / "code-indexer-python"
        repo2_index = repo2_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo2_index.mkdir(parents=True)
        self._create_valid_hnsw_index(repo2_index, vector_count=3)

        # Create database with ONLY repo1 and repo2 registered
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path,
            [
                ("python-mock", str(repo1_dir)),
                ("code-indexer-python", str(repo2_dir)),
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

            # Should ONLY check the 2 registered repos
            details = result.details
            assert details["repos_checked"] == 2

            # Should NOT report errors about "aliases", "cidx-meta", etc.
            if "repos_with_issues" in details:
                for issue in details["repos_with_issues"]:
                    repo_name = issue.get("repo", "")
                    assert repo_name not in ["aliases", "cidx-meta", ".versioned"]

            # Should report that both registered repos are healthy
            assert details["repos_with_healthy_indexes"] == 2

    @pytest.mark.asyncio
    async def test_vector_storage_only_scans_database_registered_repos(self, tmp_path):
        """
        Test that diagnostic queries database to get registered repos list.

        Ensures the fix queries golden_repos_metadata table instead of
        blindly scanning filesystem directories.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create 3 repo directories on filesystem
        repo1_dir = golden_repos_dir / "repo1"
        repo1_index = repo1_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo1_index.mkdir(parents=True)
        self._create_valid_hnsw_index(repo1_index, vector_count=5)

        repo2_dir = golden_repos_dir / "repo2"
        repo2_index = repo2_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo2_index.mkdir(parents=True)
        self._create_valid_hnsw_index(repo2_index, vector_count=3)

        repo3_dir = golden_repos_dir / "repo3"  # Exists on filesystem
        repo3_index = repo3_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo3_index.mkdir(parents=True)
        self._create_valid_hnsw_index(repo3_index, vector_count=7)

        # But ONLY register repo1 and repo2 in database
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

            # Should ONLY check the 2 registered repos (from database)
            details = result.details
            assert details["repos_checked"] == 2
            assert details["repos_with_healthy_indexes"] == 2

            # Should NOT check repo3 (not in database)
            if "repos_with_issues" in details:
                for issue in details["repos_with_issues"]:
                    assert "repo3" not in issue.get("repo", "")

    @pytest.mark.asyncio
    async def test_vector_storage_handles_empty_database_gracefully(self, tmp_path):
        """
        Test diagnostic behavior when no repos are registered in database.

        Should report 0 repos checked, not scan filesystem directories.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create filesystem directories (should be ignored)
        (golden_repos_dir / "random-dir").mkdir()
        (golden_repos_dir / "another-dir").mkdir()

        # Empty database (no registered repos)
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

            # Should report 0 repos (nothing registered)
            details = result.details
            assert details["repos_checked"] == 0
            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "no golden repositories found" in result.message.lower()

    # Helper methods

    def _create_valid_hnsw_index(self, collection_dir: Path, vector_count: int = 10):
        """Create a valid HNSW index with metadata for testing."""
        import hnswlib
        import numpy as np

        # Create HNSW index
        index = hnswlib.Index(space="cosine", dim=1024)
        index.init_index(max_elements=100000, ef_construction=200, M=16)

        # Add some vectors
        vectors = np.random.rand(vector_count, 1024).astype("float32")
        ids = list(range(vector_count))
        index.add_items(vectors, ids)

        # Save index
        hnsw_file = collection_dir / "hnsw_index.bin"
        index.save_index(str(hnsw_file))

        # Create metadata
        metadata = {
            "vector_size": 1024,
            "hnsw_index": {"vector_dim": 1024, "space": "cosine", "max_elements": 100000},
        }
        meta_file = collection_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata))

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
