"""
Unit tests for Bug #200: False positive HNSW diagnostics error on empty collections.

Bug Description:
When a collection has never been finalized (0 files indexed), the metadata has NO
hnsw_index section at all:

{
  "name": "voyage-multimodal-3",
  "vector_size": 1024,
  "created_at": "2026-02-12T18:08:08.997823",
  "quantization_range": {...},
  "unique_file_count": 0
}

The empty collection check at diagnostics_service.py:1072 does:
  vector_count = metadata.get("hnsw_index", {}).get("vector_count", None)
  if vector_count == 0:  # This fails because vector_count is None, not 0
      return None

So it falls through and reports false error: "Missing HNSW index file in [collection]"

Fix: Check for unique_file_count == 0 OR "hnsw_index" not in metadata as well.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)


class TestBug200EmptyCollectionFalsePositive:
    """Test Bug #200: False positive on empty collections without hnsw_index metadata."""

    @pytest.mark.asyncio
    async def test_empty_collection_with_no_hnsw_index_section_is_healthy(
        self, tmp_path
    ):
        """
        Test that empty collection with unique_file_count=0 but NO hnsw_index section
        is treated as healthy (not reported as missing HNSW file).

        This is the EXACT bug scenario from issue #200.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "never-finalized-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-multimodal-3"
        repo_index_dir.mkdir(parents=True)

        # Create metadata with unique_file_count=0 and NO hnsw_index section
        # (never-finalized collection - no vectors were ever indexed)
        metadata = {
            "name": "voyage-multimodal-3",
            "vector_size": 1024,
            "created_at": "2026-02-12T18:08:08.997823",
            "quantization_range": {"min": -0.75, "max": 0.75},
            "unique_file_count": 0,
            # NOTE: NO hnsw_index section at all
        }
        meta_file = repo_index_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        # NO hnsw_index.bin file (collection never finalized)

        # Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("never-finalized-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Empty collection should be healthy (no issues reported)
            # BUG: Currently reports "Missing HNSW index file in voyage-multimodal-3"
            assert result.status == DiagnosticStatus.WORKING, \
                f"Expected WORKING but got {result.status}: {result.message}"
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details.get("repos_with_issues", [])) == 0, \
                f"Should have no issues, but got: {result.details.get('repos_with_issues', [])}"

    @pytest.mark.asyncio
    async def test_empty_collection_with_unique_file_count_zero_is_healthy(
        self, tmp_path
    ):
        """
        Test that collection with unique_file_count=0 is healthy even if hnsw_index
        section exists but has vector_count > 0 (edge case).
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "edge-case-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # Edge case: unique_file_count=0 but hnsw_index section exists
        # (could happen if index was built then all files deleted)
        metadata = {
            "name": "voyage-code-3",
            "vector_size": 1024,
            "created_at": "2026-02-12T18:08:08.997823",
            "quantization_range": {"min": -0.75, "max": 0.75},
            "unique_file_count": 0,  # No files indexed
            "hnsw_index": {
                "version": 1,
                "vector_count": 5,  # Stale count (should be ignored)
                "vector_dim": 1024,
                "M": 16,
                "ef_construction": 200,
                "space": "cosine",
            },
        }
        meta_file = repo_index_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        # NO hnsw_index.bin file

        # Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("edge-case-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # unique_file_count=0 means empty, should be healthy
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details.get("repos_with_issues", [])) == 0

    @pytest.mark.asyncio
    async def test_nonempty_collection_missing_hnsw_file_reports_error(
        self, tmp_path
    ):
        """
        Regression test: Collection with unique_file_count > 0 but missing HNSW file
        should still report error.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "broken-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # Collection has files indexed but missing HNSW file (real problem)
        metadata = {
            "name": "voyage-code-3",
            "vector_size": 1024,
            "created_at": "2026-02-12T18:08:08.997823",
            "quantization_range": {"min": -0.75, "max": 0.75},
            "unique_file_count": 10,  # Has files but no HNSW
            "hnsw_index": {
                "version": 1,
                "vector_count": 10,
                "vector_dim": 1024,
                "M": 16,
                "ef_construction": 200,
                "space": "cosine",
            },
        }
        meta_file = repo_index_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        # NO hnsw_index.bin file (broken!)

        # Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("broken-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should report error (has files but missing HNSW)
            assert result.status in [DiagnosticStatus.WARNING, DiagnosticStatus.ERROR]
            assert "repos_with_issues" in result.details
            assert len(result.details["repos_with_issues"]) == 1
            assert "missing hnsw" in str(result.details["repos_with_issues"][0]).lower()

    @pytest.mark.asyncio
    async def test_collection_with_vector_count_zero_in_hnsw_index_is_healthy(
        self, tmp_path
    ):
        """
        Regression test: Existing behavior where vector_count=0 in hnsw_index section
        is treated as healthy should continue to work.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "existing-behavior-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # Metadata with hnsw_index.vector_count = 0
        metadata = {
            "name": "voyage-code-3",
            "vector_size": 1024,
            "created_at": "2026-02-12T18:08:08.997823",
            "quantization_range": {"min": -0.75, "max": 0.75},
            "unique_file_count": 0,
            "hnsw_index": {
                "version": 1,
                "vector_count": 0,  # Explicitly 0
                "vector_dim": 1024,
                "M": 16,
                "ef_construction": 200,
                "space": "cosine",
            },
        }
        meta_file = repo_index_dir / "collection_meta.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        # NO hnsw_index.bin file

        # Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("existing-behavior-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should be healthy (existing behavior)
            assert result.status == DiagnosticStatus.WORKING
            assert result.details["repos_checked"] == 1
            assert result.details["repos_with_healthy_indexes"] == 1
            assert len(result.details.get("repos_with_issues", [])) == 0

    @pytest.mark.asyncio
    async def test_collection_with_no_metadata_file_reports_error(
        self, tmp_path
    ):
        """
        Regression test: Collection with no metadata file at all should report error.
        """
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "no-metadata-repo"
        repo_index_dir = repo_dir / ".code-indexer" / "index" / "voyage-code-3"
        repo_index_dir.mkdir(parents=True)

        # NO collection_meta.json file at all
        # NO hnsw_index.bin file

        # Register repo in database
        db_path = tmp_path / "cidx_server.db"
        self._create_database_with_registered_repos(
            db_path, [("no-metadata-repo", str(repo_dir))]
        )

        with patch(
            "code_indexer.server.services.diagnostics_service.ServerConfigManager"
        ) as mock_config_manager:
            mock_config = Mock()
            mock_config.server_dir = str(tmp_path)
            mock_config_manager.return_value.load_config.return_value = mock_config

            service = DiagnosticsService(db_path=str(db_path))
            result = await service.check_vector_storage()

            # Should report error (no metadata to prove it's empty)
            assert result.status in [DiagnosticStatus.WARNING, DiagnosticStatus.ERROR]
            assert "repos_with_issues" in result.details
            assert len(result.details["repos_with_issues"]) == 1

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
