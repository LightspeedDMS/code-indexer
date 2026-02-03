"""
Unit tests for REST SCIP route backward compatibility and error handling.

Story #41: Refactor REST SCIP Routes to Use SCIPQueryService

Tests for backward compatibility, duplicate function removal, and error handling.
Following TDD methodology - these tests are written FIRST before implementation.

Story #50: Route handlers converted from async to sync for proper FastAPI threadpool execution.
The underlying service methods are sync, so using async handlers that await sync functions
causes TypeError. Sync handlers let FastAPI/uvicorn run them in the threadpool correctly.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user() -> User:
    """Create a mock user for testing."""
    return User(
        username="testuser",
        email="test@example.com",
        full_name="Test User",
        role=UserRole.NORMAL_USER,
        password_hash="hashed_password",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_scip_service():
    """Create a mock SCIPQueryService."""
    service = MagicMock()
    return service


class TestDuplicateFunctionsRemoved:
    """Tests to verify duplicate functions have been removed from scip_queries.py."""

    def test_find_scip_files_not_in_module(self):
        """AC: Verify _find_scip_files function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        # The function should not exist as a public or private function
        assert not hasattr(
            scip_queries, "_find_scip_files"
        ), "_find_scip_files should be removed from scip_queries.py"

    def test_get_accessible_repos_not_in_module(self):
        """AC: Verify _get_accessible_repos function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_get_accessible_repos"
        ), "_get_accessible_repos should be removed from scip_queries.py"

    def test_filter_scip_results_not_in_module(self):
        """AC: Verify _filter_scip_results function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_filter_scip_results"
        ), "_filter_scip_results should be removed from scip_queries.py"

    def test_get_golden_repos_dir_not_in_module(self):
        """Verify _get_golden_repos_dir function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_get_golden_repos_dir"
        ), "_get_golden_repos_dir should be removed from scip_queries.py"

    def test_filter_impact_results_not_in_module(self):
        """Verify _filter_impact_results function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_filter_impact_results"
        ), "_filter_impact_results should be removed from scip_queries.py"

    def test_filter_callchain_results_not_in_module(self):
        """Verify _filter_callchain_results function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_filter_callchain_results"
        ), "_filter_callchain_results should be removed from scip_queries.py"

    def test_filter_context_results_not_in_module(self):
        """Verify _filter_context_results function is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_filter_context_results"
        ), "_filter_context_results should be removed from scip_queries.py"

    def test_extract_repo_name_from_project_not_in_module(self):
        """Verify _extract_repo_name_from_project is removed from scip_queries.py."""
        import code_indexer.server.routers.scip_queries as scip_queries

        assert not hasattr(
            scip_queries, "_extract_repo_name_from_project"
        ), "_extract_repo_name_from_project should be removed from scip_queries.py"


class TestBackwardCompatibility:
    """Tests for REST response backward compatibility.

    Story #50: Tests converted from async to sync since route handlers are now sync.
    """

    def test_definition_response_structure_unchanged(
        self, mock_user, mock_scip_service
    ):
        """AC: Verify /scip/definition response structure is unchanged."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_definition

        mock_scip_service.find_definition.return_value = [
            {
                "symbol": "UserService",
                "project": "/path/to/project",
                "file_path": "src/services.py",
                "line": 10,
                "column": 0,
                "kind": "definition",
                "relationship": None,
                "context": "class UserService:",
            }
        ]

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.find_definition.return_value
                )
                # Sync call - no await needed
                response = get_definition(
                    request=mock_request,
                    symbol="UserService",
                    exact=False,
                    project=None,
                    current_user=mock_user,
                )

                # Verify backward-compatible response structure
                assert "success" in response
                assert "symbol" in response
                assert "total_results" in response
                assert "results" in response
                assert isinstance(response["results"], list)

                # Verify result item structure
                if response["results"]:
                    result = response["results"][0]
                    assert "symbol" in result
                    assert "project" in result
                    assert "file_path" in result
                    assert "line" in result
                    assert "column" in result
                    assert "kind" in result

    def test_references_response_structure_unchanged(
        self, mock_user, mock_scip_service
    ):
        """AC: Verify /scip/references response structure is unchanged."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_references

        mock_scip_service.find_references.return_value = []

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = []
                # Sync call - no await needed
                response = get_references(
                    request=mock_request,
                    symbol="UserService",
                    limit=100,
                    exact=False,
                    project=None,
                    current_user=mock_user,
                )

                assert "success" in response
                assert "symbol" in response
                assert "total_results" in response
                assert "results" in response

    def test_impact_response_structure_unchanged(self, mock_user, mock_scip_service):
        """AC: Verify /scip/impact response structure is unchanged."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_impact

        mock_scip_service.analyze_impact.return_value = {
            "target_symbol": "UserService",
            "depth_analyzed": 3,
            "total_affected": 1,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_impact(
                request=mock_request,
                symbol="UserService",
                depth=3,
                project=None,
                current_user=mock_user,
            )

            # Verify backward-compatible response structure
            assert "success" in response
            assert "target_symbol" in response
            assert "depth_analyzed" in response
            assert "total_affected" in response
            assert "truncated" in response
            assert "affected_symbols" in response
            assert "affected_files" in response

    def test_callchain_response_structure_unchanged(self, mock_user, mock_scip_service):
        """AC: Verify /scip/callchain response structure is unchanged."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_callchain

        mock_scip_service.trace_callchain.return_value = []

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_callchain(
                request=mock_request,
                from_symbol="A",
                to_symbol="B",
                max_depth=10,
                project=None,
                current_user=mock_user,
            )

            # Verify backward-compatible response structure
            assert "success" in response
            assert "from_symbol" in response
            assert "to_symbol" in response
            assert "total_chains_found" in response
            assert "chains" in response

    def test_context_response_structure_unchanged(self, mock_user, mock_scip_service):
        """AC: Verify /scip/context response structure is unchanged."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_context

        mock_scip_service.get_context.return_value = {
            "target_symbol": "UserService",
            "summary": "Read these files",
            "files": [],
            "total_files": 0,
            "total_symbols": 0,
            "avg_relevance": 0.0,
        }

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_context(
                request=mock_request,
                symbol="UserService",
                limit=20,
                min_score=0.0,
                project=None,
                current_user=mock_user,
            )

            # Verify backward-compatible response structure
            assert "success" in response
            assert "target_symbol" in response
            assert "summary" in response
            assert "files" in response
            assert "total_files" in response
            assert "total_symbols" in response
            assert "avg_relevance" in response


class TestErrorHandling:
    """Tests for error handling in refactored routes.

    Story #50: Tests converted from async to sync since route handlers are now sync.
    """

    def test_definition_catches_service_exception(self, mock_user, mock_scip_service):
        """Verify route catches and returns errors when service raises exception."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_definition

        mock_scip_service.find_definition.side_effect = Exception("Database error")
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Sync call - no await needed
            response = get_definition(
                request=mock_request,
                symbol="Test",
                exact=False,
                project=None,
                current_user=mock_user,
            )

            assert response["success"] is False
            assert "error" in response

    def test_references_catches_service_exception(self, mock_user, mock_scip_service):
        """Verify references route catches and returns errors."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_references

        mock_scip_service.find_references.side_effect = Exception("Query failed")
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Sync call - no await needed
            response = get_references(
                request=mock_request,
                symbol="Test",
                limit=100,
                exact=False,
                project=None,
                current_user=mock_user,
            )

            assert response["success"] is False
            assert "error" in response

    def test_impact_catches_service_exception(self, mock_user, mock_scip_service):
        """Verify impact route catches and returns errors."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_impact

        mock_scip_service.analyze_impact.side_effect = Exception("Analysis failed")
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_impact(
                request=mock_request,
                symbol="Test",
                depth=3,
                project=None,
                current_user=mock_user,
            )

            assert response["success"] is False
            assert "error" in response

    def test_callchain_catches_service_exception(self, mock_user, mock_scip_service):
        """Verify callchain route catches and returns errors."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_callchain

        mock_scip_service.trace_callchain.side_effect = Exception("Tracing failed")
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_callchain(
                request=mock_request,
                from_symbol="A",
                to_symbol="B",
                max_depth=10,
                project=None,
                current_user=mock_user,
            )

            assert response["success"] is False
            assert "error" in response

    def test_context_catches_service_exception(self, mock_user, mock_scip_service):
        """Verify context route catches and returns errors."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_context

        mock_scip_service.get_context.side_effect = Exception("Context failed")
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = get_context(
                request=mock_request,
                symbol="Test",
                limit=20,
                min_score=0.0,
                project=None,
                current_user=mock_user,
            )

            assert response["success"] is False
            assert "error" in response
