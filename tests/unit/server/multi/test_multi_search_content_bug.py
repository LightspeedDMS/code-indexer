"""
Tests for MultiSearchService include_source bug fix.

BUG: Multi-repo semantic search returns empty `content` fields because
`include_source=False` is hardcoded in _search_semantic_sync().

FIX: Change `include_source=False` to `include_source=True` to match
single-repo behavior and return actual source code content.

These tests are written FIRST (TDD) to demonstrate the bug before implementing the fix.
"""

import inspect
import pytest


class TestSemanticSearchIncludesSourceContent:
    """Test that multi-repo semantic search includes source content in results."""

    def test_semantic_search_request_has_include_source_true(self):
        """
        Multi-repo semantic search should set include_source=True.

        Bug: include_source=False was hardcoded, causing empty content fields.
        Fix: Change to include_source=True to match single-repo behavior.

        This test reads the source code to verify include_source=True is set.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        # Get the source code of the method that creates SemanticSearchRequest
        source = inspect.getsource(MultiSearchService._search_semantic_sync)

        # Should NOT find include_source=False (the bug)
        assert "include_source=False" not in source, (
            "Bug detected: include_source=False found in _search_semantic_sync. "
            "Multi-repo search won't return content. "
            "Fix: Change to include_source=True"
        )

        # Should find include_source=True (the fix)
        assert "include_source=True" in source, (
            "Fix required: include_source=True must be set in SemanticSearchRequest "
            "for content to be returned in multi-repo search results"
        )

    def test_semantic_search_request_construction_includes_source(self):
        """
        Verify the SemanticSearchRequest is constructed with include_source=True.

        This test verifies the actual request construction pattern.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        # Read full source of the method
        source = inspect.getsource(MultiSearchService._search_semantic_sync)

        # Find the SemanticSearchRequest construction
        # It should contain include_source=True, not False
        request_pattern = "SemanticSearchRequest("

        assert request_pattern in source, (
            "SemanticSearchRequest should be constructed in _search_semantic_sync"
        )

        # The construction should have include_source=True
        # Find the line with SemanticSearchRequest and check context
        lines = source.split('\n')
        in_request_block = False
        found_include_source_true = False

        for line in lines:
            if "SemanticSearchRequest(" in line:
                in_request_block = True
            if in_request_block:
                if "include_source=True" in line:
                    found_include_source_true = True
                    break
                if line.strip() == ")":
                    # Closed the constructor, stop looking
                    break

        assert found_include_source_true, (
            "SemanticSearchRequest construction must include 'include_source=True'"
        )


class TestIncludeSourceMatchesSingleRepoBehavior:
    """Test that multi-repo search matches single-repo search behavior."""

    def test_multi_repo_search_should_return_content_like_single_repo(self):
        """
        Multi-repo search should return content by default, like single-repo search.

        When users search via MCP/REST multi-repo APIs, they expect content
        in results, just like single-repo search returns.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_semantic_sync)

        # The bug was that multi-repo excluded content while single-repo included it
        # After the fix, both should include content
        assert "include_source=False" not in source, (
            "Multi-repo search should match single-repo behavior and include content"
        )
