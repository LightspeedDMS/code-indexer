"""
Unit tests for ConfigFixer indexing progress preservation (Bug #96).

Tests verify that ConfigFixer._apply_metadata_fixes() preserves indexing
progress metadata (files_processed, chunks_indexed, status) when fixing
configuration issues, preventing unnecessary full reindex.
"""

import json
import time
from datetime import datetime
from unittest.mock import Mock, patch
import pytest

from code_indexer.services.config_fixer import ConfigurationRepairer


class TestConfigFixerProgressPreservation:
    """Test ConfigurationRepairer preserves indexing progress during config fixes."""

    @pytest.fixture
    def temp_indexed_project(self, tmp_path):
        """Create a temporary project with completed indexing progress."""
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Create .code-indexer config directory
        config_dir = project_dir / ".code-indexer"
        config_dir.mkdir()

        # Create metadata.json with indexing progress
        metadata = {
            "project_id": "test-project",
            "codebase_dir": str(project_dir),
            "files_processed": 42,
            "chunks_indexed": 128,
            "status": "completed",
            "completed_files": ["file1.py", "file2.py"],
            "files_to_index": [],
            "total_files_to_index": 42,
            "current_file_index": 42,
            "failed_files": [],
            "git_available": True,
            "current_branch": "main",
            "current_commit": "abc123",
            "embedding_provider": "voyage-3",
            "last_index_timestamp": time.time() - 3600,  # 1 hour ago
            "indexed_at": datetime.now().isoformat(),
        }

        metadata_file = config_dir / "metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2))

        return {
            "project_dir": project_dir,
            "config_dir": config_dir,
            "metadata": metadata,
        }

    def test_fix_config_preserves_indexing_progress_basic(self, temp_indexed_project):
        """Verify fix-config preserves files_processed and chunks_indexed (Bug #96).

        CRITICAL: This test reproduces Bug #96 scenario:
        - Initial state: files_processed=42, chunks_indexed=128, status=completed
        - Run fix-config (e.g., for path correction)
        - Expected: Progress values preserved
        - Bug behavior: Values reset to 0, status changed to needs_indexing
        """
        config_dir = temp_indexed_project["config_dir"]
        project_dir = temp_indexed_project["project_dir"]
        original_metadata = temp_indexed_project["metadata"]

        # Create ConfigurationRepairer
        config_fixer = ConfigurationRepairer(config_dir)

        # Mock GitStateDetector to return stable git state
        with patch("code_indexer.services.config_fixer.GitStateDetector.detect_git_state") as mock_git:
            mock_git.return_value = {
                "git_available": True,
                "current_branch": "main",
                "current_commit": "abc123",
            }

            # Call _apply_metadata_fixes (internal method tested directly)
            corrected_metadata = config_fixer._apply_metadata_fixes(
                metadata=original_metadata.copy(),
                fixes=[],  # No fixes needed for this test
                config=Mock(embedding_provider="voyage-3", codebase_dir=str(project_dir)),
            )

        # CRITICAL: Indexing progress MUST be preserved
        assert corrected_metadata["files_processed"] == 42
        assert corrected_metadata["chunks_indexed"] == 128
        assert corrected_metadata["status"] == "completed"
        assert corrected_metadata["total_files_to_index"] == 42
        assert corrected_metadata["current_file_index"] == 42

        # Config fields CAN be updated
        assert corrected_metadata["project_id"] is not None
        assert corrected_metadata["codebase_dir"] == str(project_dir)

    def test_fix_config_preserves_in_progress_status(self, temp_indexed_project):
        """Verify fix-config preserves status=in_progress for interrupted operations."""
        config_dir = temp_indexed_project["config_dir"]
        project_dir = temp_indexed_project["project_dir"]

        # Modify metadata to simulate interrupted indexing
        metadata_file = config_dir / "metadata.json"
        metadata = json.loads(metadata_file.read_text())
        metadata.update({
            "files_processed": 20,
            "chunks_indexed": 60,
            "status": "in_progress",
            "current_file_index": 20,
            "total_files_to_index": 42,
            "files_to_index": ["file21.py", "file22.py"],  # Remaining files
        })
        metadata_file.write_text(json.dumps(metadata, indent=2))

        config_fixer = ConfigurationRepairer(config_dir)

        with patch("code_indexer.services.config_fixer.GitStateDetector.detect_git_state") as mock_git:
            mock_git.return_value = {
                "git_available": True,
                "current_branch": "main",
                "current_commit": "abc123",
            }

            corrected_metadata = config_fixer._apply_metadata_fixes(
                metadata=metadata.copy(),
                fixes=[],  # No fixes needed for this test
                config=Mock(embedding_provider="voyage-3", codebase_dir=str(project_dir)),
            )

        # CRITICAL: In-progress state MUST be preserved
        assert corrected_metadata["status"] == "in_progress"
        assert corrected_metadata["files_processed"] == 20
        assert corrected_metadata["chunks_indexed"] == 60
        assert corrected_metadata["current_file_index"] == 20
        assert corrected_metadata["files_to_index"] == ["file21.py", "file22.py"]

    def test_fix_config_only_updates_config_fields(self, temp_indexed_project):
        """Verify fix-config ONLY modifies configuration fields, not runtime state."""
        config_dir = temp_indexed_project["config_dir"]
        project_dir = temp_indexed_project["project_dir"]
        original_metadata = temp_indexed_project["metadata"]

        config_fixer = ConfigurationRepairer(config_dir)

        with patch("code_indexer.services.config_fixer.GitStateDetector.detect_git_state") as mock_git:
            mock_git.return_value = {
                "git_available": True,
                "current_branch": "feature",  # Different branch
                "current_commit": "xyz789",  # Different commit
            }

            corrected_metadata = config_fixer._apply_metadata_fixes(
                metadata=original_metadata.copy(),
                fixes=[],  # No fixes needed for this test
                config=Mock(embedding_provider="voyage-3-large", codebase_dir=str(project_dir)),
            )

        # Configuration fields SHOULD be updated
        assert corrected_metadata["current_branch"] == "feature"
        assert corrected_metadata["current_commit"] == "xyz789"
        assert corrected_metadata["embedding_provider"] == "voyage-3-large"

        # Runtime state fields MUST NOT be changed
        assert corrected_metadata["files_processed"] == 42
        assert corrected_metadata["chunks_indexed"] == 128
        assert corrected_metadata["status"] == "completed"
        assert corrected_metadata["completed_files"] == ["file1.py", "file2.py"]
        assert corrected_metadata["failed_files"] == []

    def test_fix_config_preserves_interrupted_operation_state(self, temp_indexed_project):
        """Verify fix-config preserves files_to_index for resumable operations.

        files_to_index contains paths for resuming interrupted indexing.
        These paths may become invalid if project is moved/cloned, but that's OK -
        they're runtime state, not configuration. Config fixer should preserve them.
        """
        config_dir = temp_indexed_project["config_dir"]
        project_dir = temp_indexed_project["project_dir"]

        # Simulate interrupted indexing with specific file paths
        metadata_file = config_dir / "metadata.json"
        metadata = json.loads(metadata_file.read_text())
        metadata.update({
            "status": "in_progress",
            "files_to_index": [
                "/old/path/file1.py",  # Potentially invalid path (from old location)
                "/old/path/file2.py",
            ],
            "current_file_index": 10,
            "total_files_to_index": 12,
        })
        metadata_file.write_text(json.dumps(metadata, indent=2))

        config_fixer = ConfigurationRepairer(config_dir)

        with patch("code_indexer.services.config_fixer.GitStateDetector.detect_git_state") as mock_git:
            mock_git.return_value = {
                "git_available": True,
                "current_branch": "main",
                "current_commit": "abc123",
            }

            corrected_metadata = config_fixer._apply_metadata_fixes(
                metadata=metadata.copy(),
                fixes=[],  # No fixes needed for this test
                config=Mock(embedding_provider="voyage-3", codebase_dir=str(project_dir)),
            )

        # CRITICAL: files_to_index MUST be preserved even with invalid paths
        # These are runtime state for resumable operations, not configuration
        assert corrected_metadata["files_to_index"] == [
            "/old/path/file1.py",
            "/old/path/file2.py",
        ]
        assert corrected_metadata["current_file_index"] == 10
        assert corrected_metadata["total_files_to_index"] == 12

    def test_fix_config_without_collection_analyzer(self, temp_indexed_project):
        """Verify fix-config works when collection_analyzer is None (filesystem backend).

        CRITICAL: This reproduces the exact bug scenario:
        - FilesystemVectorStore backend (collection_analyzer is None)
        - Original bug: Created placeholder with zeros, overwrote existing progress
        - Fixed behavior: Preserve existing progress, don't create placeholder
        """
        config_dir = temp_indexed_project["config_dir"]
        project_dir = temp_indexed_project["project_dir"]
        original_metadata = temp_indexed_project["metadata"]

        config_fixer = ConfigurationRepairer(config_dir)

        # Ensure collection_analyzer is None (simulates filesystem backend)
        assert config_fixer.collection_analyzer is None

        with patch("code_indexer.services.config_fixer.GitStateDetector.detect_git_state") as mock_git:
            mock_git.return_value = {
                "git_available": True,
                "current_branch": "main",
                "current_commit": "abc123",
            }

            corrected_metadata = config_fixer._apply_metadata_fixes(
                metadata=original_metadata.copy(),
                fixes=[],  # No fixes needed for this test
                config=Mock(embedding_provider="voyage-3", codebase_dir=str(project_dir)),
            )

        # CRITICAL: Indexing progress preserved even without collection_analyzer
        assert corrected_metadata["files_processed"] == 42
        assert corrected_metadata["chunks_indexed"] == 128
        assert corrected_metadata["status"] == "completed"
