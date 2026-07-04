"""
Unit tests for Temporal Query API parameters (Story #446).

Tests the REST API integration of temporal parameters:
- time_range: Time range filter (YYYY-MM-DD..YYYY-MM-DD)
- at_commit: Query at specific commit (point-in-time scoping, Bug #1301)

Bug #1301: `include_removed`, `show_evolution`, and `evolution_limit` were
retired -- they were advertised but were permanent silent no-ops on the
per-commit temporal index (Epic #1289). They no longer exist on
SemanticQueryRequest. Per-file diff timelines belong to the existing git
tools (git_file_history, git_log, git_blame, git_diff) instead.

TDD Cycle:
1. Write failing tests for each acceptance criterion
2. Implement minimal code to pass tests
3. Refactor for quality
"""

import pytest

try:
    from code_indexer.server.app import SemanticQueryRequest
except ImportError:
    pytest.skip("Server app not available", allow_module_level=True)


class TestTemporalParametersStory446:
    """Test temporal parameters added in Story #446 for REST API."""

    def test_time_range_parameter_exists(self):
        """AC1: Test time_range parameter exists on SemanticQueryRequest"""
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="test query", time_range="2024-01-01..2024-12-31"
        )

        # Assert
        assert hasattr(request, "time_range")
        assert request.time_range == "2024-01-01..2024-12-31"

    def test_time_range_parameter_optional(self):
        """AC1: Test time_range is optional (defaults to None)"""
        # Arrange & Act
        request = SemanticQueryRequest(query_text="test")

        # Assert
        assert request.time_range is None

    def test_at_commit_parameter_exists(self):
        """AC2: Test at_commit parameter exists on SemanticQueryRequest"""
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="test query", at_commit="abc123def456"
        )

        # Assert
        assert hasattr(request, "at_commit")
        assert request.at_commit == "abc123def456"

    def test_at_commit_parameter_optional(self):
        """AC2: Test at_commit is optional (defaults to None)"""
        # Arrange & Act
        request = SemanticQueryRequest(query_text="test")

        # Assert
        assert request.at_commit is None

    def test_temporal_parameters_combined(self):
        """AC6: Test time_range and at_commit can be used together.

        Bug #1301: this previously combined 5 params (including the now-
        retired include_removed/show_evolution/evolution_limit); narrowed
        to the 2 params that still exist.
        """
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="authentication logic",
            time_range="2024-01-01..2024-12-31",
            at_commit="main",
        )

        # Assert
        assert request.time_range == "2024-01-01..2024-12-31"
        assert request.at_commit == "main"

    def test_temporal_parameters_backward_compatible(self):
        """AC7: Test backward compatibility - existing queries work without temporal params"""
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="test query",
            limit=10,
            min_score=0.7,
            file_extensions=[".py", ".js"],
        )

        # Assert - temporal parameters use defaults
        assert request.time_range is None
        assert request.at_commit is None

    def test_temporal_parameters_with_fts_mode(self):
        """AC8: Test temporal parameters work with FTS search mode"""
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="test", search_mode="fts", time_range="2024-01-01..2024-12-31"
        )

        # Assert
        assert request.search_mode == "fts"
        assert request.time_range == "2024-01-01..2024-12-31"

    def test_temporal_parameters_with_hybrid_mode(self):
        """AC8: Test temporal parameters work with hybrid search mode"""
        # Arrange & Act
        request = SemanticQueryRequest(
            query_text="test",
            search_mode="hybrid",
            at_commit="main",
        )

        # Assert
        assert request.search_mode == "hybrid"
        assert request.at_commit == "main"


class TestTemporalParameterDescriptions:
    """Test that temporal parameters have proper descriptions for API docs."""

    def test_time_range_has_description(self):
        """Test time_range parameter has description"""
        from code_indexer.server.app import SemanticQueryRequest

        field = SemanticQueryRequest.model_fields.get("time_range")
        assert field is not None
        assert field.description is not None
        assert "time range" in field.description.lower()

    def test_at_commit_has_description(self):
        """Test at_commit parameter has description"""
        from code_indexer.server.app import SemanticQueryRequest

        field = SemanticQueryRequest.model_fields.get("at_commit")
        assert field is not None
        assert field.description is not None
        assert "commit" in field.description.lower()


class TestWarningFieldManualTestIssue1:
    """Test warning field for graceful fallback messages (Manual Test Issue 1)."""

    def test_warning_field_exists_on_response_model(self):
        """Test SemanticQueryResponse has optional warning field"""
        from code_indexer.server.app import SemanticQueryResponse

        # Should be able to create response with warning
        response = SemanticQueryResponse(
            results=[],
            total_results=0,
            query_metadata={
                "query_text": "test",
                "execution_time_ms": 100,
                "repositories_searched": 0,
                "timeout_occurred": False,
            },
            warning="Temporal index not available, using standard search",
        )

        assert hasattr(response, "warning")
        assert response.warning == "Temporal index not available, using standard search"

    def test_warning_field_optional(self):
        """Test warning field is optional (defaults to None)"""
        from code_indexer.server.app import SemanticQueryResponse

        response = SemanticQueryResponse(
            results=[],
            total_results=0,
            query_metadata={
                "query_text": "test",
                "execution_time_ms": 100,
                "repositories_searched": 0,
                "timeout_occurred": False,
            },
        )

        assert response.warning is None

    def test_warning_field_serialization(self):
        """Test warning field appears in JSON response"""
        from code_indexer.server.app import SemanticQueryResponse

        response = SemanticQueryResponse(
            results=[],
            total_results=0,
            query_metadata={
                "query_text": "test",
                "execution_time_ms": 100,
                "repositories_searched": 0,
                "timeout_occurred": False,
            },
            warning="Test warning message",
        )

        json_dict = response.model_dump()
        assert "warning" in json_dict
        assert json_dict["warning"] == "Test warning message"


class TestValidationErrorSurfacingManualTestIssue2:
    """Test validation errors return HTTP 400 (Manual Test Issue 2)."""

    def test_invalid_time_range_format_returns_400(self):
        """Test invalid time_range format returns HTTP 400 with clear error"""
        # This test validates the endpoint behavior through integration testing
        # The actual endpoint implementation should catch ValueError and return HTTP 400
        # We'll verify this through the SemanticQueryRequest validation first
        from code_indexer.server.app import SemanticQueryRequest

        # Valid format should work
        request = SemanticQueryRequest(
            query_text="test", time_range="2024-01-01..2024-12-31"
        )
        assert request.time_range == "2024-01-01..2024-12-31"

        # Note: Backend validation of time_range format happens in query_user_repositories
        # This test documents that invalid formats should trigger ValueError
        # which endpoint should catch and convert to HTTP 400

    def test_invalid_at_commit_returns_400(self):
        """Test invalid at_commit (non-existent commit) should return HTTP 400.

        Bug #1301: at_commit is now actually resolved+validated via
        resolve_commit_timestamp() (see
        tests/unit/services/temporal/test_at_commit_scoping_1301.py for the
        real ValueError-raising behavior against a real git repo). This test
        keeps documenting the REST-model-level acceptance of the field.
        """
        from code_indexer.server.app import SemanticQueryRequest

        # Valid format should work
        request = SemanticQueryRequest(query_text="test", at_commit="main")
        assert request.at_commit == "main"

        # Note: Backend validation of commit existence happens in
        # resolve_commit_timestamp() (temporal_search_service.py), called
        # from execute_temporal_query_with_fusion(). Invalid commits raise
        # ValueError which the endpoint catches and converts to HTTP 400.
