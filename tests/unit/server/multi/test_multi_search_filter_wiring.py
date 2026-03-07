"""
TDD tests for Story #376: Wire search filters through multi-repo omni-search path.

Verifies:
- MultiSearchRequest model accepts new filter fields (exclude_language, exclude_path, accuracy)
- MultiSearchRequest defaults new fields to None
- MultiSearchService._search_semantic_sync passes all filter fields to SemanticSearchRequest
- handlers.py _omni_search_code passes filter params to MultiSearchRequest

NOTE: Uses source code inspection (like test_multi_search_content_bug.py) for wiring
verification to avoid circular import and local-import patching issues.
"""

import inspect


class TestMultiSearchRequestNewFields:
    """Test that MultiSearchRequest model accepts new filter fields."""

    def test_multi_search_request_accepts_exclude_language(self):
        """MultiSearchRequest should accept exclude_language field."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
            exclude_language="python",
        )
        assert request.exclude_language == "python"

    def test_multi_search_request_accepts_exclude_path(self):
        """MultiSearchRequest should accept exclude_path field."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
            exclude_path="*/tests/*",
        )
        assert request.exclude_path == "*/tests/*"

    def test_multi_search_request_accepts_accuracy(self):
        """MultiSearchRequest should accept accuracy field."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
            accuracy="high",
        )
        assert request.accuracy == "high"

    def test_multi_search_request_defaults_exclude_language_to_none(self):
        """MultiSearchRequest.exclude_language should default to None."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
        )
        assert request.exclude_language is None

    def test_multi_search_request_defaults_exclude_path_to_none(self):
        """MultiSearchRequest.exclude_path should default to None."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
        )
        assert request.exclude_path is None

    def test_multi_search_request_defaults_accuracy_to_none(self):
        """MultiSearchRequest.accuracy should default to None."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
        )
        assert request.accuracy is None

    def test_multi_search_request_all_new_fields_together(self):
        """MultiSearchRequest should accept all new filter fields simultaneously."""
        from code_indexer.server.multi.models import MultiSearchRequest

        request = MultiSearchRequest(
            repositories=["repo1", "repo2"],
            query="authentication",
            search_type="semantic",
            limit=5,
            language="python",
            path_filter="*/src/*",
            exclude_language="javascript",
            exclude_path="*/tests/*",
            accuracy="high",
        )
        assert request.language == "python"
        assert request.path_filter == "*/src/*"
        assert request.exclude_language == "javascript"
        assert request.exclude_path == "*/tests/*"
        assert request.accuracy == "high"


class TestMultiSearchServiceFilterWiring:
    """Test that MultiSearchService._search_semantic_sync passes all filter fields.

    Uses source code inspection to verify wiring, following the pattern
    established in test_multi_search_content_bug.py. This avoids issues
    with patching locally-imported classes.
    """

    def _get_search_semantic_source(self):
        """Get source code of _search_semantic_sync."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        return inspect.getsource(MultiSearchService._search_semantic_sync)

    def test_search_semantic_passes_language(self):
        """_search_semantic_sync should pass language= to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        assert "language=request.language" in source, (
            "SemanticSearchRequest construction must include language=request.language"
        )

    def test_search_semantic_passes_path_filter(self):
        """_search_semantic_sync should pass path_filter= to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        assert "path_filter=request.path_filter" in source, (
            "SemanticSearchRequest construction must include path_filter=request.path_filter"
        )

    def test_search_semantic_passes_exclude_language(self):
        """_search_semantic_sync should pass exclude_language= to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        assert "exclude_language=request.exclude_language" in source, (
            "SemanticSearchRequest construction must include exclude_language=request.exclude_language"
        )

    def test_search_semantic_passes_exclude_path(self):
        """_search_semantic_sync should pass exclude_path= to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        assert "exclude_path=request.exclude_path" in source, (
            "SemanticSearchRequest construction must include exclude_path=request.exclude_path"
        )

    def test_search_semantic_passes_accuracy(self):
        """_search_semantic_sync should pass accuracy= to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        assert "accuracy=request.accuracy" in source, (
            "SemanticSearchRequest construction must include accuracy=request.accuracy"
        )

    def test_search_semantic_passes_all_filters(self):
        """_search_semantic_sync should pass ALL filter fields to SemanticSearchRequest."""
        source = self._get_search_semantic_source()
        for field in ["language", "path_filter", "exclude_language", "exclude_path", "accuracy"]:
            assert f"{field}=request.{field}" in source, (
                f"SemanticSearchRequest construction must include {field}=request.{field}"
            )


class TestOmniSearchHandlerFilterWiring:
    """Test that _omni_search_code in handlers.py passes filter params to MultiSearchRequest.

    Uses source code inspection to verify wiring.
    """

    def _get_omni_search_source(self):
        """Get source code of _omni_search_code."""
        from code_indexer.server.mcp.handlers import _omni_search_code

        return inspect.getsource(_omni_search_code)

    def test_omni_search_passes_exclude_language(self):
        """_omni_search_code should pass exclude_language to MultiSearchRequest."""
        source = self._get_omni_search_source()
        assert 'exclude_language=params.get("exclude_language")' in source, (
            "MultiSearchRequest construction must include exclude_language from params"
        )

    def test_omni_search_passes_exclude_path(self):
        """_omni_search_code should pass exclude_path to MultiSearchRequest."""
        source = self._get_omni_search_source()
        assert 'exclude_path=params.get("exclude_path")' in source, (
            "MultiSearchRequest construction must include exclude_path from params"
        )

    def test_omni_search_passes_accuracy(self):
        """_omni_search_code should pass accuracy to MultiSearchRequest."""
        source = self._get_omni_search_source()
        assert 'accuracy=params.get("accuracy"' in source, (
            "MultiSearchRequest construction must include accuracy from params"
        )
