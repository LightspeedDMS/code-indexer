"""Unit tests for daemon error handling and propagation.

Tests that errors in semantic search (e.g., VoyageAI API key errors) are properly
propagated to the CLI rather than silently swallowed.
"""

import pytest
from unittest.mock import patch
from io import StringIO


class TestExecuteSemanticSearchErrorPropagation:
    """Test that _execute_semantic_search propagates errors in timing_info."""

    @pytest.fixture
    def service(self):
        """Create daemon service with mocked eviction thread."""
        from code_indexer.daemon.service import CIDXDaemonService

        service = CIDXDaemonService()
        yield service

        # Cleanup
        service.eviction_thread.stop()
        service.eviction_thread.join(timeout=1)

    @pytest.fixture
    def mock_project_path(self, tmp_path):
        """Create mock project with index structure."""
        project_path = tmp_path / "test_project"
        project_path.mkdir()

        # Create .code-indexer directory structure
        config_dir = project_path / ".code-indexer"
        config_dir.mkdir()
        index_dir = config_dir / "index"
        index_dir.mkdir()

        return project_path

    def test_execute_semantic_search_returns_error_in_timing_on_api_key_failure(
        self, service, mock_project_path
    ):
        """When semantic search fails with API key error, error should be in timing_info."""
        api_key_error = ValueError(
            "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable."
        )

        # Mock BackendFactory.create to raise the API key error
        # Must mock at the source module path, not where it's imported
        with patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            side_effect=api_key_error,
        ):
            results, timing_info = service._execute_semantic_search(
                str(mock_project_path), "test query", limit=10
            )

        # Verify error is propagated
        assert results == []
        assert "error" in timing_info
        assert "VoyageAI API key" in timing_info["error"]

    def test_execute_semantic_search_returns_error_in_timing_on_generic_exception(
        self, service, mock_project_path
    ):
        """When semantic search fails with any exception, error should be in timing_info."""
        generic_error = RuntimeError("Connection failed to embedding service")

        # Mock BackendFactory.create to raise the generic error
        # Must mock at the source module path, not where it's imported
        with patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            side_effect=generic_error,
        ):
            results, timing_info = service._execute_semantic_search(
                str(mock_project_path), "test query", limit=10
            )

        # Verify error is propagated
        assert results == []
        assert "error" in timing_info
        assert "Connection failed" in timing_info["error"]


class TestExposedQueryErrorPropagation:
    """Test that exposed_query includes errors in response."""

    @pytest.fixture
    def service(self):
        """Create daemon service with mocked eviction thread."""
        from code_indexer.daemon.service import CIDXDaemonService

        service = CIDXDaemonService()
        yield service

        # Cleanup
        service.eviction_thread.stop()
        service.eviction_thread.join(timeout=1)

    @pytest.fixture
    def mock_project_path(self, tmp_path):
        """Create mock project with index structure."""
        project_path = tmp_path / "test_project"
        project_path.mkdir()

        # Create .code-indexer directory structure
        config_dir = project_path / ".code-indexer"
        config_dir.mkdir()
        index_dir = config_dir / "index"
        index_dir.mkdir()

        return project_path

    def test_exposed_query_includes_error_from_semantic_search(
        self, service, mock_project_path
    ):
        """exposed_query should include error from _execute_semantic_search in response."""
        error_message = (
            "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable."
        )

        # Mock _execute_semantic_search to return error in timing_info
        with patch.object(
            service,
            "_execute_semantic_search",
            return_value=([], {"error": error_message}),
        ):
            result = service.exposed_query(str(mock_project_path), "test query")

        # Verify error is in the response
        assert result["results"] == []
        assert "error" in result
        assert error_message in result["error"]

    def test_exposed_query_passes_through_timing_info_with_error(
        self, service, mock_project_path
    ):
        """exposed_query should preserve both timing and error info."""
        timing_with_error = {
            "query_time_ms": 5,
            "error": "Some error message",
        }

        with patch.object(
            service,
            "_execute_semantic_search",
            return_value=([], timing_with_error),
        ):
            result = service.exposed_query(str(mock_project_path), "test query")

        # Both timing and error should be preserved
        assert result["timing"]["query_time_ms"] == 5
        assert result["error"] == "Some error message"


class TestDisplayResultsErrorHandling:
    """Test that _display_results shows error messages to users."""

    def test_display_results_shows_error_when_present(self):
        """_display_results should display error message when present in response."""
        from code_indexer.cli_daemon_delegation import _display_results
        from rich.console import Console

        # Create console that captures output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=120)

        # Response with error
        response = {
            "results": [],
            "timing": {},
            "error": "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable.",
        }

        # Patch the console in the module
        with patch("code_indexer.cli_daemon_delegation.console", console):
            _display_results(response)

        captured = output.getvalue()

        # Should show error, not "No results found"
        assert "VoyageAI API key" in captured or "Search failed" in captured
        # Should NOT just say "No results found"
        # Note: After fix, we expect error message to be displayed

    def test_display_results_shows_api_key_guidance(self):
        """_display_results should show guidance for API key errors."""
        from code_indexer.cli_daemon_delegation import _display_results
        from rich.console import Console

        # Create console that captures output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=120)

        # Response with API key error
        response = {
            "results": [],
            "timing": {},
            "error": "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable.",
        }

        # Patch the console in the module
        with patch("code_indexer.cli_daemon_delegation.console", console):
            _display_results(response)

        captured = output.getvalue()

        # Should provide actionable guidance about setting API key
        assert "VOYAGE_API_KEY" in captured

    def test_display_results_normal_no_results_unchanged(self):
        """_display_results should still show 'No results found' when there's no error."""
        from code_indexer.cli_daemon_delegation import _display_results
        from rich.console import Console

        # Create console that captures output
        output = StringIO()
        console = Console(file=output, force_terminal=True, width=120)

        # Response with no error, just empty results
        response = {
            "results": [],
            "timing": {},
        }

        # Patch the console in the module
        with patch("code_indexer.cli_daemon_delegation.console", console):
            _display_results(response)

        captured = output.getvalue()

        # Should show "No results found" for normal empty results
        assert "No results found" in captured
