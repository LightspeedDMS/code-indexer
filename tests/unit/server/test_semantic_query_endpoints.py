"""
Unit tests for semantic query API endpoint - Story 7.

Tests the semantic query functionality:
- POST /api/query (semantic query with authentication)
- Query processing with user isolation
- Repository filtering and validation
- Result formatting and background job integration
- Resource limits and error handling
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from datetime import datetime, timezone

from code_indexer.server.app import create_app
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.mark.e2e
class TestSemanticQueryEndpoint:
    """Test POST /api/query endpoint for semantic search."""

    @pytest.fixture
    def client(self):
        """Create FastAPI test client."""
        app = create_app()
        return TestClient(app)

    @pytest.fixture
    def mock_admin_user(self):
        """Mock admin user for authentication."""
        return User(
            username="admin",
            password_hash="$2b$12$hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )

    @pytest.fixture
    def mock_normal_user(self):
        """Mock normal user for authentication."""
        return User(
            username="testuser",
            password_hash="$2b$12$hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_with_valid_request(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query with valid authenticated request returns 200."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        mock_return_value = {
            "results": [
                {
                    "file_path": "/path/to/file.py",
                    "line_number": 1,
                    "code_snippet": "def test(): pass",
                    "similarity_score": 0.85,
                    "repository_alias": "my-repo",
                }
            ],
            "total_results": 1,
            "query_metadata": {
                "query_text": "test function",
                "execution_time_ms": 100,
                "repositories_searched": 1,
                "timeout_occurred": False,
            },
        }

        # Patch the REAL semantic_query_manager instance captured as a
        # closure parameter by register_query_routes (Story #409-style
        # extraction); code_indexer.server.app.semantic_query_manager never
        # reaches it.
        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            return_value=mock_return_value,
        ) as mock_query:
            response = client.post(
                "/api/query",
                json={"query_text": "test function", "limit": 10},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "total_results" in data
        assert "query_metadata" in data
        assert data["total_results"] == 1
        assert len(data["results"]) == 1
        assert data["results"][0]["similarity_score"] == 0.85

        mock_query.assert_called_once()
        call_kwargs = mock_query.call_args.kwargs
        assert call_kwargs["username"] == "testuser"
        assert call_kwargs["query_text"] == "test function"
        assert call_kwargs["repository_alias"] is None
        assert call_kwargs["limit"] == 10
        assert call_kwargs["min_score"] is None
        assert call_kwargs["file_extensions"] is None

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_with_repository_filter(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query with repository filter parameter."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        mock_return_value = {
            "results": [],
            "total_results": 0,
            "query_metadata": {
                "query_text": "test",
                "execution_time_ms": 50,
                "repositories_searched": 1,
                "timeout_occurred": False,
            },
        }

        # Patch the REAL semantic_query_manager instance (closure-capture
        # class -- see test_semantic_query_with_valid_request above).
        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            return_value=mock_return_value,
        ) as mock_query:
            response = client.post(
                "/api/query",
                json={
                    "query_text": "test",
                    "repository_alias": "specific-repo",
                    "limit": 5,
                    "min_score": 0.7,
                },
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 200

        mock_query.assert_called_once()
        call_kwargs = mock_query.call_args.kwargs
        assert call_kwargs["username"] == "testuser"
        assert call_kwargs["query_text"] == "test"
        assert call_kwargs["repository_alias"] == "specific-repo"
        assert call_kwargs["limit"] == 5
        assert call_kwargs["min_score"] == 0.7
        assert call_kwargs["file_extensions"] is None

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_as_background_job(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query submitted as background job when async=true."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        # Patch the REAL semantic_query_manager instance (closure-capture
        # class -- see test_semantic_query_with_valid_request above).
        with patch.object(
            client.app.state.semantic_query_manager,
            "submit_query_job",
            return_value="job-123",
        ) as mock_submit:
            response = client.post(
                "/api/query",
                json={"query_text": "complex query", "async_query": True},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert "message" in data
        assert data["job_id"] == "job-123"

        mock_submit.assert_called_once_with(
            username="testuser",
            query_text="complex query",
            repository_alias=None,
            limit=10,
            min_score=None,
            file_extensions=None,
        )

    def test_semantic_query_without_authentication(self, client):
        """Test semantic query without authentication returns 401."""
        response = client.post("/api/query", json={"query_text": "test"})

        # Per MCP spec RFC 9728, missing credentials return 401, not 403.
        assert response.status_code == 401

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_with_invalid_parameters(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query with invalid parameters returns 422.

        #1707 Finding 3 cleanup: no manager patch needed -- Pydantic
        validation rejects the empty query_text before the route handler
        (and its semantic_query_manager closure) ever runs.
        """
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        # Test empty query text
        response = client.post(
            "/api/query",
            json={"query_text": ""},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 422  # FastAPI validation error

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_with_no_repositories(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query when user has no activated repositories returns 400.

        #1707 Finding 3 cleanup: patch the REAL semantic_query_manager
        instance (closure-capture class -- see
        test_semantic_query_with_valid_request above) and assert the mock
        is actually called, instead of relying on "testuser" coincidentally
        having no real activated repositories.
        """
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )

        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            side_effect=SemanticQueryError("No activated repositories found"),
        ) as mock_query:
            response = client.post(
                "/api/query",
                json={"query_text": "test"},
                headers={"Authorization": "Bearer test-token"},
            )

        mock_query.assert_called_once()

        # Verify error response
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "No activated repositories" in data["detail"]

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_with_invalid_repository(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query with invalid repository alias returns 404."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )

        # Patch the REAL semantic_query_manager instance (closure-capture
        # class -- see test_semantic_query_with_valid_request above).
        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            side_effect=SemanticQueryError("Repository 'invalid-repo' not found"),
        ):
            response = client.post(
                "/api/query",
                json={"query_text": "test", "repository_alias": "invalid-repo"},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data
        assert "not found" in data["detail"]

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_timeout_error(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query timeout returns 408."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )

        # Patch the REAL semantic_query_manager instance (closure-capture
        # class -- see test_semantic_query_with_valid_request above).
        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            side_effect=SemanticQueryError("Query timed out"),
        ):
            response = client.post(
                "/api/query",
                json={"query_text": "test"},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 408
        data = response.json()
        assert "detail" in data
        assert "timed out" in data["detail"]

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_internal_error(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query with internal error returns 500."""
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        # Patch the REAL semantic_query_manager instance (closure-capture
        # class -- see test_semantic_query_with_valid_request above).
        with patch.object(
            client.app.state.semantic_query_manager,
            "query_user_repositories",
            side_effect=Exception("Internal search error"),
        ):
            response = client.post(
                "/api/query",
                json={"query_text": "test"},
                headers={"Authorization": "Bearer test-token"},
            )

        # Verify error response
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data
        assert "search error" in data["detail"]

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_validates_limit_range(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query validates limit parameter range.

        #1707 Finding 3 cleanup: no manager patch needed -- Pydantic
        validation rejects the out-of-range limit before the route handler
        (and its semantic_query_manager closure) ever runs.
        """
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        # Test negative limit
        response = client.post(
            "/api/query",
            json={"query_text": "test", "limit": -1},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 422  # Pydantic validation error

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_semantic_query_validates_min_score_range(
        self,
        mock_dep_user_manager,
        mock_jwt_manager,
        client,
        mock_normal_user,
    ):
        """Test semantic query validates min_score parameter range.

        #1707 Finding 3 cleanup: no manager patch needed -- Pydantic
        validation rejects the out-of-range min_score before the route
        handler (and its semantic_query_manager closure) ever runs.
        """
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_normal_user

        # Test invalid min_score > 1.0
        response = client.post(
            "/api/query",
            json={"query_text": "test", "min_score": 1.5},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 422  # Pydantic validation error
