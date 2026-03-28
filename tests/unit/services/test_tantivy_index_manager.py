"""
Tests for TantivyIndexManager - full-text search index management.

Tests ensure proper Tantivy integration for building FTS indexes
alongside semantic vector indexes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager


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
            assert heap_size == 1_000_000_000, (
                "IndexWriter should use fixed 1GB heap size"
            )

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
            for i, path in enumerate(
                ["src/auth.py", "src/user.py", "tests/test_auth.py"]
            ):
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
            assert sorted(result) == [
                "src/auth.py",
                "src/user.py",
                "tests/test_auth.py",
            ]

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


class TestSanitizeFtsQuery:
    """Tests for the module-level sanitize_fts_query() pure function."""

    def test_sanitize_fts_query_no_quotes(self):
        """Input with no quotes is returned unchanged (fast path)."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("hello world") == "hello world"
        assert sanitize_fts_query("def authenticate") == "def authenticate"
        assert sanitize_fts_query("") == ""

    def test_sanitize_fts_query_even_quotes(self):
        """Balanced (even) number of quotes returned unchanged — phrase search preserved."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        # Two quotes: valid phrase search like "hello world"
        assert sanitize_fts_query('"hello world"') == '"hello world"'
        # Four quotes: two separate phrase terms
        assert sanitize_fts_query('"foo" "bar"') == '"foo" "bar"'

    def test_sanitize_fts_query_odd_single_quote(self):
        """Single unmatched quote causes all quotes to be stripped entirely."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query('hello "world') == "hello world"
        assert sanitize_fts_query('"authenticate') == "authenticate"
        assert sanitize_fts_query('test"') == "test"

    def test_sanitize_fts_query_odd_three_quotes(self):
        """Three quotes (odd count) causes all quotes to be stripped."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        # Three quotes: odd, all stripped
        assert sanitize_fts_query('"foo" "bar') == "foo bar"
        assert sanitize_fts_query('a "b" "c') == "a b c"

    def test_sanitize_fts_query_bare_or_operator(self):
        """Bare OR alone is lowercased so Tantivy treats it as literal text."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("OR") == "or"

    def test_sanitize_fts_query_bare_and_operator(self):
        """Bare AND alone is lowercased so Tantivy treats it as literal text."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("AND") == "and"

    def test_sanitize_fts_query_bare_not_operator(self):
        """Bare NOT alone is lowercased so Tantivy treats it as literal text."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("NOT") == "not"

    def test_sanitize_fts_query_trailing_boolean(self):
        """Trailing OR/AND without right operand is lowercased to prevent syntax error."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("term OR") == "term or"
        assert sanitize_fts_query("term AND") == "term and"

    def test_sanitize_fts_query_leading_boolean(self):
        """Leading OR/AND without left operand is lowercased to prevent syntax error."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("OR term") == "or term"
        assert sanitize_fts_query("AND term") == "and term"

    def test_sanitize_fts_query_adjacent_booleans(self):
        """Adjacent boolean operators are all lowercased to prevent syntax error."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert sanitize_fts_query("term OR AND other") == "term or and other"

    def test_sanitize_fts_query_valid_boolean_preserved(self):
        """Valid boolean queries with operands on both sides are left unchanged."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        # These are valid Tantivy queries — must not be modified
        assert sanitize_fts_query("term1 OR term2") == "term1 OR term2"
        assert sanitize_fts_query("term1 AND term2") == "term1 AND term2"
        assert sanitize_fts_query("NOT term1") == "NOT term1"

    def test_sanitize_fts_query_mixed_case_boolean(self):
        """Mixed-case operators like 'Or', 'aNd' are not Tantivy operators — left unchanged."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        # Tantivy only treats all-uppercase OR/AND/NOT as boolean operators
        assert sanitize_fts_query("Or term") == "Or term"
        assert sanitize_fts_query("aNd term") == "aNd term"
        assert sanitize_fts_query("term oR other") == "term oR other"

    def test_sanitize_fts_query_boolean_with_quotes(self):
        """Combined: odd quote + bare boolean operator — both bugs fixed together."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        # Odd quote strips all quotes, then bare OR is lowercased
        result = sanitize_fts_query('"term OR')
        # After quote stripping: "term OR" → all quotes removed → "term OR"
        # Then OR is trailing (no right operand) → "term or"
        assert result == "term or"

    # Phase 3 tests: special syntax characters

    def test_sanitize_fts_query_colon_replaced_with_space(self):
        """Colon replaced with space to prevent Tantivy field reference interpretation.

        'com.cdk.recreation:SomeClass' causes Tantivy ValueError 'Field does not exist'
        because Tantivy interprets field:value syntax. Colon must become a space.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("com.cdk.recreation:SomeClass")
        assert result == "com.cdk.recreation SomeClass"

    def test_sanitize_fts_query_double_colon_replaced(self):
        """Double colon (C++ scope resolution) replaced with spaces.

        'std::vector' causes Tantivy SyntaxError. Both colons must become spaces.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("std::vector")
        assert "std" in result
        assert "vector" in result
        assert ":" not in result

    def test_sanitize_fts_query_parentheses_stripped(self):
        """Parentheses stripped to prevent Tantivy grouping syntax interpretation.

        'foo(bar)' causes Tantivy SyntaxError because parentheses are grouping operators.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("foo(bar)")
        assert "(" not in result
        assert ")" not in result
        assert "foo" in result
        assert "bar" in result

    def test_sanitize_fts_query_brackets_stripped(self):
        """Square brackets stripped to prevent Tantivy range syntax interpretation.

        'test[0]' causes Tantivy SyntaxError because brackets are range operators.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("test[0]")
        assert "[" not in result
        assert "]" not in result
        assert "test" in result
        assert "0" in result

    def test_sanitize_fts_query_braces_stripped(self):
        """Curly braces stripped to prevent Tantivy range syntax interpretation.

        '{a TO z}' causes Tantivy 'Unsupported query' ValueError.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("{a TO z}")
        assert "{" not in result
        assert "}" not in result

    def test_sanitize_fts_query_mixed_special_chars(self):
        """Mixed special characters in a realistic code identifier are all sanitized."""
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("com.cdk:Class(method)")
        assert ":" not in result
        assert "(" not in result
        assert ")" not in result
        assert "com.cdk" in result

    def test_sanitize_fts_query_safe_chars_preserved(self):
        """Dot, tilde, asterisk are safe and must NOT be removed.

        These characters have useful semantics in FTS queries (wildcards, fuzzy, etc.).
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        assert "." in sanitize_fts_query("com.example.Class")
        assert "*" in sanitize_fts_query("test*")
        assert "~" in sanitize_fts_query("roam~")

    def test_sanitize_fts_query_phase3_with_phase1_phase2(self):
        """Phase 3 works correctly together with Phase 1 (quotes) and Phase 2 (booleans).

        A query with unmatched quotes AND colons should have all phases applied.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query('"com.example:Class')
        assert '"' not in result
        assert ":" not in result
        assert "com.example" in result
        assert "Class" in result

    def test_sanitize_fts_query_colon_before_trailing_boolean_lowercased(self):
        """Phase ordering: syntax escaping (colon) must run BEFORE boolean validation.

        'ns:OR' as a single colon-separated token:
          - OLD order (boolean first, syntax second): boolean sees 'ns:OR' as one token,
            does nothing; then syntax replaces colon giving 'ns OR' (trailing OR UNSAFE).
          - NEW order (syntax first, boolean second): syntax replaces colon giving 'ns OR';
            then boolean sees trailing OR with no right operand and lowercases to 'ns or' (SAFE).

        Bug #357: With the correct phase order, trailing OR from colon-escaped identifiers
        is sanitized to lowercase, preventing Tantivy parse errors.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("ns:OR")
        # After colon escaping: "ns OR" (trailing OR)
        # After boolean validation of trailing OR: "ns or" (safe literal)
        assert result == "ns or", f"Expected 'ns or', got {result!r}"
        assert ":" not in result
        # OR must be lowercased (it's trailing with no right operand)
        assert "OR" not in result

    def test_sanitize_fts_query_ns_colon_or_colon_value(self):
        """'ns:OR:value' - colon escaping first, then boolean validation sees valid OR.

        With correct phase order (syntax before boolean):
          - Phase 1 (quotes): no change
          - Phase 2 (syntax): 'ns:OR:value' → 'ns OR value' (colons become spaces)
          - Phase 3 (boolean): OR has 'ns' on left and 'value' on right → valid, preserved

        Result: 'ns OR value' — a valid Tantivy boolean query.
        """
        from code_indexer.services.tantivy_index_manager import sanitize_fts_query

        result = sanitize_fts_query("ns:OR:value")
        assert ":" not in result
        assert "ns" in result
        assert "value" in result
        # OR is valid (operands on both sides after colon escaping) — preserved uppercase
        assert result == "ns OR value", f"Expected 'ns OR value', got {result!r}"


class TestSearchDefenseInDepth:
    """Tests for defense-in-depth ValueError handling in search().

    Bug #357: _build_search_query() can raise ValueError from Tantivy's parse_query()
    for edge-case syntax that Phase 3 sanitization misses. The search() method must
    catch these and return empty results rather than propagating the error and causing
    QUERY-MIGRATE error bursts across all repositories.

    CRITICAL: Intentional ValueErrors raised before _build_search_query() (for
    edit_distance out-of-range at line 650 and invalid regex at line 681) must still
    propagate to the caller — only Tantivy parse errors from _build_search_query()
    should be silently caught.
    """

    def _make_manager(self, tmpdir: str) -> "TantivyIndexManager":
        """Helper: create and initialize a TantivyIndexManager with one document."""
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"
        manager = TantivyIndexManager(index_dir=index_dir)
        manager.initialize_index()
        manager.add_document(
            {
                "path": "src/example.py",
                "content": "def authenticate user token",
                "content_raw": "def authenticate user token",
                "identifiers": ["authenticate", "user", "token"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
        return manager

    def test_search_returns_empty_when_build_query_raises_value_error(self, caplog):
        """search() returns empty list and logs warning when _build_search_query() raises ValueError.

        Defense-in-depth: even if a query slips through Phase 3 sanitization and
        Tantivy's parse_query() raises ValueError, search() must not propagate it —
        it must return [] and log a warning. This prevents the QUERY-MIGRATE error
        burst cascade across repositories.
        """
        import logging

        with tempfile.TemporaryDirectory() as tmpdir:
            from unittest.mock import patch

            manager = self._make_manager(tmpdir)

            # Simulate _build_search_query raising ValueError (as Tantivy parse_query does)
            with patch.object(
                manager,
                "_build_search_query",
                side_effect=ValueError("Syntax Error in query"),
            ):
                with caplog.at_level(
                    logging.WARNING,
                    logger="code_indexer.services.tantivy_index_manager",
                ):
                    results = manager.search(query_text="some:bad:query")

            # Must return empty list, not raise
            assert results == []
            # Must log a warning about the failure
            assert any(
                "warning" in r.levelname.lower() or "Syntax Error" in r.message
                for r in caplog.records
            )

    def test_search_still_propagates_invalid_edit_distance_value_error(self):
        """ValueError from edit_distance validation (before _build_search_query) still propagates.

        The defense-in-depth catch is ONLY for _build_search_query(). The intentional
        ValueError raised at line 650 for invalid edit_distance must still propagate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            # edit_distance=5 is out of range (0-3) — must raise ValueError
            with pytest.raises(ValueError, match="edit_distance must be 0-3"):
                manager.search(query_text="test", edit_distance=5)

    def test_search_still_propagates_invalid_regex_value_error(self):
        """ValueError from invalid regex (before _build_search_query) still propagates.

        The defense-in-depth catch is ONLY for _build_search_query(). The intentional
        ValueError raised at line 681 for an invalid regex pattern must still propagate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            # Invalid regex: unmatched bracket — must raise ValueError
            with pytest.raises(ValueError, match="[Ii]nvalid regex"):
                manager.search(query_text="[invalid regex", use_regex=True)


class TestBuildSearchQuerySanitization:
    """Integration tests verifying sanitize_fts_query is applied in _build_search_query."""

    def _make_manager(self, tmpdir: str) -> "TantivyIndexManager":
        """Helper: create and initialize a TantivyIndexManager."""
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"
        manager = TantivyIndexManager(index_dir=index_dir)
        manager.initialize_index()
        # Add one document so the index is not empty and queries can execute
        manager.add_document(
            {
                "path": "src/example.py",
                "content": "def authenticate user token",
                "content_raw": "def authenticate user token",
                "identifiers": ["authenticate", "user", "token"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
        return manager

    def test_build_search_query_sanitizes_unmatched_quote_exact_single_term(self):
        """Exact single-term path: unmatched quote stripped before parse_query() call.

        Without sanitization, parse_query('"authenticate') raises a Tantivy
        SyntaxError. With sanitization it should succeed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Single term with a leading unmatched quote — would raise SyntaxError
            # without sanitization. After sanitization it becomes 'authenticate'.
            # Must not raise.
            result = manager._build_search_query(
                query_text='"authenticate',
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_sanitizes_unmatched_quote_exact_multi_term(self):
        """Exact multi-term AND path: unmatched quote stripped before per-term parse_query() calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Two terms, trailing unmatched quote on second — sanitized to 'user token'
            result = manager._build_search_query(
                query_text='user token"',
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_sanitizes_unmatched_quote_fuzzy_single_term(self):
        """Fuzzy single-term path: unmatched quote stripped before fuzzy_term_query() call."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Fuzzy single term with unmatched quote — sanitized to 'authenticate'
            result = manager._build_search_query(
                query_text='"authenticate',
                search_field="content",
                edit_distance=1,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_sanitizes_unmatched_quote_fuzzy_multi_term(self):
        """Fuzzy multi-term path: unmatched quote stripped before per-term fuzzy_term_query() calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Two fuzzy terms with trailing unmatched quote — sanitized to 'user token'
            result = manager._build_search_query(
                query_text='user token"',
                search_field="content",
                edit_distance=1,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_preserves_matched_phrase_query(self):
        """Even-quote input (phrase search) passes through _build_search_query unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Balanced quotes: valid phrase search, must not be stripped
            result = manager._build_search_query(
                query_text='"authenticate user"',
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_sanitizes_bare_boolean_operator(self):
        """Bare boolean operator (just 'OR') must not crash _build_search_query.

        Without sanitization, Tantivy raises 'Syntax Error: OR' when
        parse_query() receives a bare boolean operator with no operands.
        With sanitization it should succeed by treating 'or' as a literal term.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Bare operator — would raise Tantivy SyntaxError without sanitization
            result = manager._build_search_query(
                query_text="OR",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_build_search_query_sanitizes_trailing_boolean(self):
        """Trailing boolean operator ('term OR') must not crash _build_search_query.

        Without sanitization, Tantivy raises 'Syntax Error: OR' for a trailing
        operator with no right operand. With sanitization it should succeed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Trailing operator — would raise Tantivy SyntaxError without sanitization
            result = manager._build_search_query(
                query_text="authenticate OR",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None


class TestContainsValidBooleanOps:
    """Unit tests for the module-level _contains_valid_boolean_ops() pure function."""

    def test_valid_or(self):
        """'term1 OR term2' has valid OR with operands on both sides — returns True."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("term1 OR term2") is True

    def test_valid_and(self):
        """'term1 AND term2' has valid AND with operands on both sides — returns True."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("term1 AND term2") is True

    def test_valid_not(self):
        """Bare 'NOT term1' is NOT valid for Tantivy parse_query — returns False.

        Tantivy rejects queries with only exclusion terms ('Only excluding terms
        given'). NOT is only valid in compound expressions like 'auth NOT token'
        where a positive term also exists.
        """
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("NOT term1") is False

    def test_compound_not(self):
        """'auth NOT token' has NOT with positive term on left — returns True."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("auth NOT token") is True

    def test_chained_ops(self):
        """'a OR b AND c' contains valid boolean ops — returns True."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("a OR b AND c") is True

    def test_multiple_ors(self):
        """'a OR b OR c' contains multiple valid OR ops — returns True."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("a OR b OR c") is True

    def test_no_operators(self):
        """'hello world' has no boolean operators — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("hello world") is False

    def test_single_term(self):
        """'hello' is a single term with no operators — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("hello") is False

    def test_empty_string(self):
        """Empty string has no operators — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("") is False

    def test_lowercase_or(self):
        """'term or other' uses lowercase 'or' which is not a Tantivy operator — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("term or other") is False

    def test_trailing_or(self):
        """'term OR' has trailing OR with no right operand — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("term OR") is False

    def test_leading_or(self):
        """'OR term' has leading OR with no left operand — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("OR term") is False

    def test_adjacent_operators(self):
        """'term OR AND other' has OR with an operator on its right (not a valid operand) — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("term OR AND other") is False

    def test_bare_not(self):
        """'NOT' alone has no right operand — returns False."""
        from code_indexer.services.tantivy_index_manager import (
            _contains_valid_boolean_ops,
        )

        assert _contains_valid_boolean_ops("NOT") is False


class TestBuildSearchQueryBoolean:
    """Integration tests verifying _build_search_query correctly routes boolean queries."""

    def _make_manager(self, tmpdir: str) -> "TantivyIndexManager":
        """Helper: create and initialize a TantivyIndexManager with auth/login documents."""
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"
        manager = TantivyIndexManager(index_dir=index_dir)
        manager.initialize_index()
        # Add documents containing "auth" (for OR/AND testing)
        manager.add_document(
            {
                "path": "src/auth.py",
                "content": "def authenticate user token",
                "content_raw": "def authenticate user token",
                "identifiers": ["authenticate", "user", "token"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        # Add document containing "login" (for OR testing - matches OR but not AND)
        manager.add_document(
            {
                "path": "src/login.py",
                "content": "def login user password",
                "content_raw": "def login user password",
                "identifiers": ["login", "user", "password"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
        return manager

    def test_or_query_returns_valid_query(self):
        """'auth OR login' with edit_distance=0 produces a valid Tantivy query object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            result = manager._build_search_query(
                query_text="auth OR login",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_and_query_returns_valid_query(self):
        """'auth AND login' with edit_distance=0 produces a valid Tantivy query object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            result = manager._build_search_query(
                query_text="auth AND login",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_not_query_returns_valid_query(self):
        """Bare 'NOT token' falls through to per-term AND path — no crash, valid query.

        Tantivy's parse_query() rejects queries with only exclusion terms
        ('Only excluding terms given'). Bare 'NOT token' must NOT be routed
        through the boolean parse_query() path. Instead it falls through to
        the multi-word AND path which treats 'NOT' and 'token' as two literal
        terms to match.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # Bare "NOT token" must not crash — falls through to per-term path
            result = manager._build_search_query(
                query_text="NOT token",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_fuzzy_boolean_degrades_gracefully(self, caplog):
        """'auth OR login' with edit_distance=1 degrades to fuzzy-AND with warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import logging

            import tantivy
            from tantivy import Query as TantivyQuery

            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.services.tantivy_index_manager",
            ):
                result = manager._build_search_query(
                    query_text="auth OR login",
                    search_field="content",
                    edit_distance=1,
                    tantivy=tantivy,
                    TantivyQuery=TantivyQuery,
                )
            assert result is not None
            assert any("Boolean operators ignored" in msg for msg in caplog.messages)

    def test_non_boolean_preserves_and_semantics(self):
        """'hello world' (no boolean ops) still uses existing AND semantics path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            result = manager._build_search_query(
                query_text="hello world",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None

    def test_search_or_returns_union_results(self):
        """Full search() with 'auth OR login' returns documents matching EITHER term."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            # "auth OR login" should match both auth.py (contains "authenticate") AND
            # login.py (contains "login") — union semantics
            results = manager.search(
                query_text="authenticate OR login", edit_distance=0
            )

            # Should find documents from both files
            paths = [r["path"] for r in results]
            assert any("auth.py" in p for p in paths), (
                f"Expected auth.py in results for 'authenticate OR login', got: {paths}"
            )
            assert any("login.py" in p for p in paths), (
                f"Expected login.py in results for 'authenticate OR login', got: {paths}"
            )

    def test_search_non_boolean_unchanged(self):
        """Full search() with 'hello world' (no boolean ops) returns only docs with both terms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            # "hello world" — neither term exists in our test docs, so 0 results
            results = manager.search(query_text="hello world", edit_distance=0)
            assert results == [], (
                f"Expected no results for 'hello world' with AND semantics, got: {results}"
            )

    def test_compound_not_query_returns_valid_query(self):
        """Compound 'authenticate NOT token' routes through boolean parse_query path.

        'authenticate NOT token' has a positive term ('authenticate') on the left
        of NOT, so _contains_valid_boolean_ops() returns True and the query is
        passed to parse_query(). Tantivy accepts this because there is at least
        one positive inclusion term.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._make_manager(tmpdir)

            import tantivy
            from tantivy import Query as TantivyQuery

            # "authenticate NOT token": positive term on left, so boolean path is used
            result = manager._build_search_query(
                query_text="authenticate NOT token",
                search_field="content",
                edit_distance=0,
                tantivy=tantivy,
                TantivyQuery=TantivyQuery,
            )
            assert result is not None
