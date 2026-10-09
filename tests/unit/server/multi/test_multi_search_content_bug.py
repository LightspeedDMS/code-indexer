"""
Tests for MultiSearchService include_source bug fix.

BUG: Multi-repo semantic search returned empty `content` fields because
`include_source=False` was hardcoded in the multi-repo request construction.

FIX: `include_source=True`, matching single-repo behavior. Since #2047 the
request is built by the ONE shared builder every door uses
(server/query/filtered_search.filtered_semantic_search), to which
MultiSearchService._search_semantic_sync delegates; these tests pin both.
"""

import inspect


def _semantic_request_source() -> str:
    """Source of the code that builds the multi-repo semantic request,
    after checking _search_semantic_sync delegates to it."""
    from code_indexer.server.multi.multi_search_service import MultiSearchService
    from code_indexer.server.query.filtered_search import filtered_semantic_search

    method_source = inspect.getsource(MultiSearchService._search_semantic_sync)
    assert "filtered_semantic_search(" in method_source, (
        "_search_semantic_sync must build its request through the shared "
        "filtered_semantic_search"
    )
    return inspect.getsource(filtered_semantic_search)


class TestSemanticSearchIncludesSourceContent:
    """Test that multi-repo semantic search includes source content in results."""

    def test_semantic_search_request_has_include_source_true(self):
        """
        Multi-repo semantic search should set include_source=True.

        Bug: include_source=False was hardcoded, causing empty content fields.
        Fix: Change to include_source=True to match single-repo behavior.
        """
        source = _semantic_request_source()

        # Should NOT find include_source=False (the bug)
        assert "include_source=False" not in source, (
            "Bug detected: include_source=False found in the semantic request "
            "construction. Multi-repo search won't return content."
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
        source = _semantic_request_source()

        request_pattern = "SemanticSearchRequest("
        assert request_pattern in source, (
            "SemanticSearchRequest should be constructed by the shared builder"
        )

        # The construction should have include_source=True
        lines = source.split("\n")
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
        source = _semantic_request_source()

        # The bug was that multi-repo excluded content while single-repo included it
        # After the fix, both should include content
        assert "include_source=False" not in source, (
            "Multi-repo search should match single-repo behavior and include content"
        )
