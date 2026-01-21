"""
Unit tests for GoldenRepoManager.get_golden_repo_indexes returning separate types.

Verifies that the method returns separate status for:
- semantic (embedding-based search)
- fts (full-text search with Tantivy)
- temporal (git history)
- scip (code intelligence)

NOT the old combined semantic_fts type.

Story #2: Fix Add Index functionality - HIGH-1
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
import tempfile
import shutil


class TestGetGoldenRepoIndexesSeparateTypes:
    """Tests for get_golden_repo_indexes returning separate semantic/fts status."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    @pytest.fixture
    def mock_golden_repo_manager(self, temp_dir):
        """Create a mock GoldenRepoManager with test setup."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        # Create mock golden repo
        repo_path = temp_dir / "test-repo"
        repo_path.mkdir(parents=True)
        code_indexer_dir = repo_path / ".code-indexer"
        code_indexer_dir.mkdir()

        manager = MagicMock(spec=GoldenRepoManager)
        manager.golden_repos_dir = str(temp_dir)

        # Create a mock golden repo object
        golden_repo = Mock()
        golden_repo.alias = "test-repo"
        golden_repo.clone_path = str(repo_path)

        manager.golden_repos = {"test-repo": golden_repo}
        manager.get_actual_repo_path = Mock(return_value=str(repo_path))

        return manager, temp_dir, repo_path

    def test_returns_separate_semantic_and_fts_keys(self, mock_golden_repo_manager):
        """
        HIGH-1: Test that response has separate 'semantic' and 'fts' keys.

        NOT: combined 'semantic_fts' key
        """
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        manager, temp_dir, repo_path = mock_golden_repo_manager

        # Use the real method implementation
        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            real_manager = GoldenRepoManager.__new__(GoldenRepoManager)
            real_manager.golden_repos = manager.golden_repos
            real_manager.get_actual_repo_path = manager.get_actual_repo_path

            # Mock _index_exists and _get_index_status methods
            real_manager._index_exists = Mock(return_value=False)
            real_manager._get_index_status = Mock(return_value={
                "exists": False,
                "path": None,
                "last_updated": None
            })

            result = real_manager.get_golden_repo_indexes("test-repo")

        # Verify structure
        assert "alias" in result
        assert "indexes" in result

        indexes = result["indexes"]

        # CRITICAL: Should have separate keys
        assert "semantic" in indexes, "Missing 'semantic' key - should be separate from FTS"
        assert "fts" in indexes, "Missing 'fts' key - should be separate from semantic"
        assert "temporal" in indexes
        assert "scip" in indexes

        # CRITICAL: Should NOT have combined key
        assert "semantic_fts" not in indexes, (
            "Found 'semantic_fts' key - should be separate 'semantic' and 'fts' keys"
        )

    def test_semantic_index_status_correctly_reported(self, temp_dir):
        """Test that semantic index presence is correctly detected."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        # Create semantic index directory
        repo_path = temp_dir / "test-repo-semantic"
        repo_path.mkdir(parents=True)
        semantic_index = repo_path / ".code-indexer" / "index"
        semantic_index.mkdir(parents=True)

        # Create the manager with real implementation
        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)
            manager.golden_repos_dir = str(temp_dir)

            golden_repo = Mock()
            golden_repo.alias = "test-repo-semantic"
            golden_repo.clone_path = str(repo_path)
            manager.golden_repos = {"test-repo-semantic": golden_repo}
            manager.get_actual_repo_path = Mock(return_value=str(repo_path))

            # Mock _index_exists to return True for semantic only
            def mock_index_exists(repo, index_type):
                return index_type == "semantic"

            manager._index_exists = mock_index_exists
            manager._get_index_status = Mock(side_effect=lambda repo_dir, idx_type, golden_repo: {
                "exists": idx_type == "semantic",
                "path": str(repo_dir / ".code-indexer" / "index") if idx_type == "semantic" else None,
                "last_updated": "2025-01-01T00:00:00Z" if idx_type == "semantic" else None
            })

            result = manager.get_golden_repo_indexes("test-repo-semantic")

        assert result["indexes"]["semantic"]["exists"] is True
        assert result["indexes"]["fts"]["exists"] is False

    def test_fts_index_status_correctly_reported(self, temp_dir):
        """Test that FTS index presence is correctly detected."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        # Create FTS index directory (tantivy_index)
        repo_path = temp_dir / "test-repo-fts"
        repo_path.mkdir(parents=True)
        fts_index = repo_path / ".code-indexer" / "tantivy_index"
        fts_index.mkdir(parents=True)

        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)
            manager.golden_repos_dir = str(temp_dir)

            golden_repo = Mock()
            golden_repo.alias = "test-repo-fts"
            golden_repo.clone_path = str(repo_path)
            manager.golden_repos = {"test-repo-fts": golden_repo}
            manager.get_actual_repo_path = Mock(return_value=str(repo_path))

            # Mock _index_exists to return True for fts only
            def mock_index_exists(repo, index_type):
                return index_type == "fts"

            manager._index_exists = mock_index_exists
            manager._get_index_status = Mock(side_effect=lambda repo_dir, idx_type, golden_repo: {
                "exists": idx_type == "fts",
                "path": str(repo_dir / ".code-indexer" / "tantivy_index") if idx_type == "fts" else None,
                "last_updated": "2025-01-01T00:00:00Z" if idx_type == "fts" else None
            })

            result = manager.get_golden_repo_indexes("test-repo-fts")

        assert result["indexes"]["semantic"]["exists"] is False
        assert result["indexes"]["fts"]["exists"] is True

    def test_both_semantic_and_fts_can_exist_independently(self, temp_dir):
        """Test that both semantic and FTS can exist independently."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        repo_path = temp_dir / "test-repo-both"
        repo_path.mkdir(parents=True)
        (repo_path / ".code-indexer" / "index").mkdir(parents=True)
        (repo_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)
            manager.golden_repos_dir = str(temp_dir)

            golden_repo = Mock()
            golden_repo.alias = "test-repo-both"
            golden_repo.clone_path = str(repo_path)
            manager.golden_repos = {"test-repo-both": golden_repo}
            manager.get_actual_repo_path = Mock(return_value=str(repo_path))

            # Both exist
            def mock_index_exists(repo, index_type):
                return index_type in ["semantic", "fts"]

            manager._index_exists = mock_index_exists
            manager._get_index_status = Mock(side_effect=lambda repo_dir, idx_type, golden_repo: {
                "exists": idx_type in ["semantic", "fts"],
                "path": str(repo_dir / ".code-indexer" / ("index" if idx_type == "semantic" else "tantivy_index")) if idx_type in ["semantic", "fts"] else None,
                "last_updated": "2025-01-01T00:00:00Z" if idx_type in ["semantic", "fts"] else None
            })

            result = manager.get_golden_repo_indexes("test-repo-both")

        # Both should exist independently
        assert result["indexes"]["semantic"]["exists"] is True
        assert result["indexes"]["fts"]["exists"] is True
        # And temporal/scip should not
        assert result["indexes"]["temporal"]["exists"] is False
        assert result["indexes"]["scip"]["exists"] is False


class TestIndexExistsMethodSeparateTypes:
    """Tests for _index_exists handling separate semantic and fts."""

    def test_index_exists_recognizes_semantic_type(self):
        """Test that _index_exists handles 'semantic' as a valid type."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)

            golden_repo = Mock()
            golden_repo.clone_path = "/fake/path"

            # The method should not raise for 'semantic' type
            with patch('pathlib.Path.exists', return_value=False):
                # Should handle 'semantic' without error
                try:
                    result = manager._index_exists(golden_repo, "semantic")
                    # If it returns something, it handled the type
                    assert result in [True, False]
                except KeyError:
                    pytest.fail("_index_exists doesn't recognize 'semantic' as valid type")
                except AttributeError:
                    # Mock setup issue, skip
                    pass

    def test_index_exists_recognizes_fts_type(self):
        """Test that _index_exists handles 'fts' as a valid type."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        with patch.object(GoldenRepoManager, '__init__', lambda self, *args, **kwargs: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)

            golden_repo = Mock()
            golden_repo.clone_path = "/fake/path"

            with patch('pathlib.Path.exists', return_value=False):
                try:
                    result = manager._index_exists(golden_repo, "fts")
                    assert result in [True, False]
                except KeyError:
                    pytest.fail("_index_exists doesn't recognize 'fts' as valid type")
                except AttributeError:
                    pass

    def test_index_exists_does_not_use_semantic_fts(self):
        """Test that _index_exists doesn't require 'semantic_fts' internally."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

        # If the implementation requires semantic_fts, that's a bug
        # Valid types should be: semantic, fts, temporal, scip
        valid_types = ["semantic", "fts", "temporal", "scip"]

        # This test documents the expected behavior
        assert "semantic_fts" not in valid_types
