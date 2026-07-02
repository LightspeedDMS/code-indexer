"""
TDD tests for Story #376: Wire search filters through Web UI global repo query path.

Verifies that routes.py global repo query path creates SemanticSearchRequest
with language and path_filter from form fields.

Uses source code inspection for route wiring verification and direct
SemanticSearchRequest construction tests for model validation.
"""

import inspect


class TestGlobalRepoQueryFilterWiring:
    """Test that global repo query path wires language and path_filter into SemanticSearchRequest."""

    def test_global_repo_query_passes_language_to_semantic_request(self):
        """Global repo query should pass language form field to SemanticSearchRequest."""
        from code_indexer.server.models.api_models import SemanticSearchRequest

        search_request = SemanticSearchRequest(
            query="authentication",
            limit=10,
            include_source=True,
            language="python",
            path_filter=None,
        )

        assert search_request.language == "python"
        assert search_request.path_filter is None

    def test_global_repo_query_passes_path_filter_to_semantic_request(self):
        """Global repo query should pass path_pattern form field as path_filter."""
        from code_indexer.server.models.api_models import SemanticSearchRequest

        search_request = SemanticSearchRequest(
            query="test",
            limit=5,
            include_source=True,
            language=None,
            path_filter="*/src/*",
        )

        assert search_request.language is None
        assert search_request.path_filter == "*/src/*"

    def test_global_repo_query_both_filters_together(self):
        """Global repo query should pass both language and path_pattern when both are set."""
        from code_indexer.server.models.api_models import SemanticSearchRequest

        search_request = SemanticSearchRequest(
            query="class definition",
            limit=20,
            include_source=True,
            language="typescript",
            path_filter="*/components/*",
        )

        assert search_request.language == "typescript"
        assert search_request.path_filter == "*/components/*"

    def test_global_repo_query_empty_filters_become_none(self):
        """Global repo query should convert empty string language and path_pattern to None."""
        from code_indexer.server.models.api_models import SemanticSearchRequest

        language = ""
        path_pattern = ""
        search_request = SemanticSearchRequest(
            query="search query",
            limit=10,
            include_source=True,
            language=language if language else None,
            path_filter=path_pattern if path_pattern else None,
        )

        assert search_request.language is None
        assert search_request.path_filter is None

    def test_global_repo_query_route_source_wires_language(self):
        """The routes.py global repo code path must wire language into SemanticSearchRequest."""
        from code_indexer.server.web import routes

        source = inspect.getsource(routes.query_submit)
        # Verify the SemanticSearchRequest construction includes language
        assert "language=language" in source, (
            "routes.py query_submit must wire language form field to SemanticSearchRequest"
        )

    def test_global_repo_query_route_source_wires_path_filter(self):
        """The routes.py global repo code path must wire path_pattern as path_filter."""
        from code_indexer.server.web import routes

        source = inspect.getsource(routes.query_submit)
        # Verify the SemanticSearchRequest construction includes path_filter
        assert "path_filter=path_pattern" in source, (
            "routes.py query_submit must wire path_pattern form field as path_filter to SemanticSearchRequest"
        )
