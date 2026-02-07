"""Unit tests for check_hnsw_health MCP handler."""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.services.hnsw_health_service import HealthCheckResult


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user for testing."""
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_regular_user():
    """Create a mock regular user for testing."""
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)  # query_repos permission
    return user


@pytest.fixture
def mock_health_result():
    """Create a mock HealthCheckResult."""
    return HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        index_path="/path/to/index.bin",
        file_size_bytes=1024000,
        errors=[],
        check_duration_ms=45.5,
        from_cache=False,
    )


class TestCheckHnswHealthHandlerRegistration:
    """Test that check_hnsw_health is registered in the MCP tool system."""

    def test_handler_registered_in_handler_registry(self):
        """Test that check_hnsw_health handler is registered in HANDLER_REGISTRY."""
        assert "check_hnsw_health" in HANDLER_REGISTRY
        assert callable(HANDLER_REGISTRY["check_hnsw_health"])


class TestCheckHnswHealthHandler:
    """Test check_hnsw_health MCP handler."""

    def test_handler_returns_health_for_valid_repository(
        self, mock_regular_user, mock_health_result
    ):
        """Test that handler returns HealthCheckResult for valid repository."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}

        # Mock the golden repo manager
        mock_repo = Mock()
        mock_repo.clone_path = "/path/to/repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            # Mock the singleton getter to return a mock service
            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(return_value=mock_health_result)
                mock_getter.return_value = mock_service

                result = check_hnsw_health(params, mock_regular_user)

                # Verify MCP response structure
                assert "content" in result
                assert len(result["content"]) == 1
                assert result["content"][0]["type"] == "text"

                # Parse response data
                response_data = json.loads(result["content"][0]["text"])
                assert response_data["success"] is True
                assert "health" in response_data

                # Verify health data
                health = response_data["health"]
                assert health["valid"] is True
                assert health["file_exists"] is True
                assert health["readable"] is True
                assert health["loadable"] is True
                assert health["element_count"] == 1000

    def test_handler_returns_error_for_unknown_repository(self, mock_regular_user):
        """Test that handler returns error for unknown repository."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "nonexistent-repo", "force_refresh": False}

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            # Simulate repository not found
            mock_manager.get_golden_repo = Mock(return_value=None)

            result = check_hnsw_health(params, mock_regular_user)

            # Verify MCP response structure
            assert "content" in result
            assert result["content"][0]["type"] == "text"

            # Parse response data
            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is False
            assert "error" in response_data
            assert "not found" in response_data["error"].lower()

    def test_force_refresh_parameter_works(
        self, mock_regular_user, mock_health_result
    ):
        """Test that force_refresh parameter is passed to HNSWHealthService."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": True}

        mock_repo = Mock()
        mock_repo.clone_path = "/path/to/repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(return_value=mock_health_result)
                mock_getter.return_value = mock_service

                check_hnsw_health(params, mock_regular_user)

                # Verify force_refresh was passed
                mock_service.check_health.assert_called_once()
                call_kwargs = mock_service.check_health.call_args[1]
                assert call_kwargs["force_refresh"] is True

    def test_handler_handles_missing_force_refresh_parameter(
        self, mock_regular_user, mock_health_result
    ):
        """Test that handler handles missing force_refresh parameter (defaults to False)."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo"}  # No force_refresh

        mock_repo = Mock()
        mock_repo.clone_path = "/path/to/repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(return_value=mock_health_result)
                mock_getter.return_value = mock_service

                check_hnsw_health(params, mock_regular_user)

                # Verify force_refresh defaults to False
                call_kwargs = mock_service.check_health.call_args[1]
                assert call_kwargs["force_refresh"] is False

    def test_handler_constructs_correct_index_path(
        self, mock_regular_user, mock_health_result
    ):
        """Test that handler constructs correct index path."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}

        mock_repo = Mock()
        mock_repo.clone_path = "/home/user/repos/test-repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(return_value=mock_health_result)
                mock_getter.return_value = mock_service

                check_hnsw_health(params, mock_regular_user)

                # Verify correct index path construction
                call_kwargs = mock_service.check_health.call_args[1]
                expected_path = str(
                    Path(mock_repo.clone_path)
                    / ".code-indexer"
                    / "index"
                    / "default"
                    / "index.bin"
                )
                assert call_kwargs["index_path"] == expected_path

    def test_handler_handles_service_exception(self, mock_regular_user):
        """Test that handler handles exceptions from HNSWHealthService."""
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}

        mock_repo = Mock()
        mock_repo.clone_path = "/path/to/repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(
                    side_effect=Exception("Health check failed")
                )
                mock_getter.return_value = mock_service

                result = check_hnsw_health(params, mock_regular_user)

                # Verify error response
                response_data = json.loads(result["content"][0]["text"])
                assert response_data["success"] is False
                assert "error" in response_data
                assert "Health check failed" in response_data["error"]
