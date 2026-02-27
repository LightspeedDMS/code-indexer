"""
Tests for TantivyIndexManager - full-text search index management.

Tests ensure proper Tantivy integration for building FTS indexes
alongside semantic vector indexes.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestTantivyIndexManager:
    """Test TantivyIndexManager core functionality."""

    def test_tantivy_index_manager_initialization(self):
        """Test TantivyIndexManager can be instantiated with index directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            # This should fail initially - TantivyIndexManager doesn't exist yet
            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            assert manager is not None
            assert manager.index_dir == index_dir

    def test_schema_creation_with_required_fields(self):
        """Test that Tantivy schema is created with all required fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            schema = manager.get_schema()

            # Required fields per acceptance criterion #5
            required_fields = [
                "path",  # stored field
                "content",  # tokenized field for FTS
                "content_raw",  # stored field for raw content
                "identifiers",  # simple tokenizer for exact identifier matches
                "line_start",  # u64 indexed
                "line_end",  # u64 indexed
                "language",  # facet field
            ]

            for field in required_fields:
                assert field in schema, f"Schema should contain required field: {field}"

    def test_index_directory_creation(self):
        """Test that index directory is created with proper permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Directory should be created
            assert index_dir.exists(), "Index directory should be created"
            assert index_dir.is_dir(), "Index path should be a directory"

            # Should be readable and writable
            assert (index_dir / ".").exists(), "Should have proper permissions"

    def test_fixed_heap_size_configuration(self):
        """Test that IndexWriter is configured with fixed 1GB heap size."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Get the writer and check heap size configuration
            heap_size = manager.get_writer_heap_size()
            assert (
                heap_size == 1_000_000_000
            ), "IndexWriter should use fixed 1GB heap size"

    def test_add_document_to_index(self):
        """Test adding a document to the FTS index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Add a test document
            doc = {
                "path": "test.py",
                "content": "def hello(): print('world')",
                "content_raw": "def hello(): print('world')",
                "identifiers": ["hello", "print"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }

            manager.add_document(doc)
            manager.commit()

            # Verify document was added
            assert manager.get_document_count() > 0

    def test_atomic_commit_prevents_corruption(self):
        """Test that commits are atomic to prevent index corruption."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Add multiple documents
            docs = [
                {
                    "path": f"test{i}.py",
                    "content": f"def func{i}(): pass",
                    "content_raw": f"def func{i}(): pass",
                    "identifiers": [f"func{i}"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "python",
                }
                for i in range(5)
            ]

            for doc in docs:
                manager.add_document(doc)

            # Commit should be atomic - either all documents are indexed or none
            manager.commit()
            count_after_commit = manager.get_document_count()
            assert count_after_commit == 5

            # Close first manager to release lock
            manager.close()

            # Simulate failure scenario - add docs and explicitly rollback
            manager2 = TantivyIndexManager(index_dir=index_dir)
            manager2.initialize_index(create_new=False)
            manager2.add_document(
                {
                    "path": "test_fail.py",
                    "content": "fail",
                    "content_raw": "fail",
                    "identifiers": ["fail"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "python",
                }
            )
            # Explicitly rollback before close
            manager2.rollback()
            manager2.close()

            # Re-open index - should still have original 5 docs (rollback discarded the 6th)
            manager3 = TantivyIndexManager(index_dir=index_dir)
            manager3.initialize_index(create_new=False)
            assert manager3.get_document_count() == 5

    def test_graceful_failure_if_tantivy_not_installed(self):
        """Test that missing Tantivy library results in clear error message."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            # Mock tantivy import failure
            with patch.dict("sys.modules", {"tantivy": None}):
                from code_indexer.services.tantivy_index_manager import (
                    TantivyIndexManager,
                )

                with pytest.raises(ImportError) as exc_info:
                    manager = TantivyIndexManager(index_dir=index_dir)
                    manager.initialize_index()

                assert "tantivy" in str(exc_info.value).lower()
                assert any(
                    word in str(exc_info.value).lower()
                    for word in ["install", "pip", "not found", "missing"]
                )

    def test_metadata_tracking_index_creation(self):
        """Test that metadata indicates FTS index availability."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            metadata = manager.get_metadata()

            # Acceptance criterion #6: metadata tracking
            assert metadata["fts_enabled"] is True
            assert metadata["fts_index_available"] is True
            assert metadata["tantivy_version"] == "0.25.0"
            assert metadata["schema_version"] == "1.0"
            assert "created_at" in metadata
            assert metadata["index_path"] == str(index_dir)

    def test_error_handling_permission_denied(self):
        """Test graceful handling of permission errors."""
        # Create a read-only directory
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"
            index_dir.mkdir(parents=True, exist_ok=True)

            # Make directory read-only
            import os

            os.chmod(index_dir, 0o444)

            try:
                from code_indexer.services.tantivy_index_manager import (
                    TantivyIndexManager,
                )

                manager = TantivyIndexManager(index_dir=index_dir)

                with pytest.raises(PermissionError) as exc_info:
                    manager.initialize_index()

                # Should have clear error message
                assert "permission" in str(exc_info.value).lower()
            finally:
                # Restore permissions for cleanup
                os.chmod(index_dir, 0o755)

    def test_rollback_on_indexing_failure(self):
        """Test that index can be rolled back if indexing fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Add valid documents
            manager.add_document(
                {
                    "path": "test.py",
                    "content": "valid",
                    "content_raw": "valid",
                    "identifiers": ["valid"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "python",
                }
            )
            manager.commit()
            initial_count = manager.get_document_count()

            # Try to add invalid document and trigger rollback
            try:
                manager.add_document(
                    {
                        "path": "invalid.py",
                        # Missing required fields intentionally
                    }
                )
                manager.commit()
            except Exception:
                manager.rollback()

            # Count should remain unchanged after rollback
            assert manager.get_document_count() == initial_count

    def test_get_all_indexed_paths_returns_correct_paths(self):
        """get_all_indexed_paths() returns all paths using searcher.search() (not segment_readers).

        This test verifies the v0.25.0-compatible implementation. The old
        implementation used segment_readers() which does not exist in tantivy-py
        v0.25.0. The new implementation must use searcher.search(all_query, num_docs).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Add documents with distinct paths
            for i, path in enumerate(["src/auth.py", "src/user.py", "tests/test_auth.py"]):
                manager.add_document(
                    {
                        "path": path,
                        "content": f"def func{i}(): pass",
                        "content_raw": f"def func{i}(): pass",
                        "identifiers": [f"func{i}"],
                        "line_start": 1,
                        "line_end": 1,
                        "language": "python",
                    }
                )
            manager.commit()

            # get_all_indexed_paths() must return all 3 paths without using segment_readers
            result = manager.get_all_indexed_paths()
            assert sorted(result) == ["src/auth.py", "src/user.py", "tests/test_auth.py"]

    def test_get_all_indexed_paths_deduplicates(self):
        """get_all_indexed_paths() returns unique paths even when multiple docs share a path.

        When a file has multiple indexed chunks (line ranges), they all share the
        same path value. The method must deduplicate and return each path once.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Add multiple docs with the SAME path (simulating multi-chunk file)
            shared_path = "src/big_module.py"
            for i in range(3):
                manager.add_document(
                    {
                        "path": shared_path,
                        "content": f"chunk {i} content",
                        "content_raw": f"chunk {i} content",
                        "identifiers": [f"chunk{i}"],
                        "line_start": i * 50 + 1,
                        "line_end": i * 50 + 50,
                        "language": "python",
                    }
                )
            manager.commit()

            # Must return the path exactly once (deduplicated)
            result = manager.get_all_indexed_paths()
            assert result == [shared_path], (
                f"Expected exactly ['{shared_path}'], got {result}"
            )

    def test_get_all_indexed_paths_empty_index(self):
        """get_all_indexed_paths() returns empty list when index has no documents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()
            # No documents added

            result = manager.get_all_indexed_paths()
            assert result == []

    def test_get_all_indexed_paths_does_not_use_segment_readers(self):
        """get_all_indexed_paths() must not call segment_readers() - API absent in v0.25.0.

        Uses a MagicMock searcher that raises AttributeError if segment_readers is
        accessed, simulating the tantivy-py v0.25.0 staging environment. The new
        implementation must return results using searcher.search() instead.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from unittest.mock import MagicMock, PropertyMock
            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            manager.add_document(
                {
                    "path": "src/foo.py",
                    "content": "def foo(): pass",
                    "content_raw": "def foo(): pass",
                    "identifiers": ["foo"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "python",
                }
            )
            manager.commit()

            # Reload to get a fresh committed state, then build a mock searcher
            # that raises AttributeError on segment_readers (v0.25.0 behavior)
            manager._index.reload()
            real_searcher = manager._index.searcher()

            mock_searcher = MagicMock(wraps=real_searcher)
            # Make segment_readers raise AttributeError like v0.25.0
            type(mock_searcher).segment_readers = PropertyMock(
                side_effect=AttributeError(
                    "'tantivy.tantivy.Searcher' object has no attribute 'segment_readers'"
                )
            )

            # Patch the index to return our mock searcher
            original_index = manager._index
            mock_index = MagicMock(wraps=original_index)
            mock_index.searcher.return_value = mock_searcher
            manager._index = mock_index

            # Must NOT raise AttributeError - new impl does not call segment_readers
            result = manager.get_all_indexed_paths()
            assert "src/foo.py" in result
