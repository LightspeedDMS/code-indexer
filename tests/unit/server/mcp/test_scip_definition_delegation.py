"""
Unit tests for scip_definition MCP handler delegation to SCIPQueryService.

Story #40: Refactor MCP SCIP Handlers to Use SCIPQueryService

Following TDD methodology - these tests are written FIRST before implementation.
"""

import json
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


class TestSCIPDefinitionDelegatesToService:
    """Tests for scip_definition handler delegation to SCIPQueryService."""

    def test_scip_definition_calls_service_find_definition(
        self, mock_user: User
    ) -> None:
        """AC: scip_definition handler delegates to SCIPQueryService.find_definition()."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = MagicMock()
        mock_service.find_definition.return_value = [
            {
                "symbol": "UserService",
                "project": "repo-a",
                "file_path": "src/services.py",
                "line": 45,
                "column": 6,
                "kind": "definition",
                "relationship": None,
                "context": "class UserService:",
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "UserService", "exact": False}
            response = scip_definition(params, mock_user)

            # Verify service method was called with correct parameters
            mock_service.find_definition.assert_called_once_with(
                symbol="UserService",
                exact=False,
                repository_alias=None,
                username="testuser",
            )

            # Verify MCP response format is unchanged
            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["symbol"] == "UserService"
            assert len(data["results"]) == 1

    def test_scip_definition_passes_repository_alias_to_service(
        self, mock_user: User
    ) -> None:
        """Verify repository_alias is passed to service."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = MagicMock()
        mock_service.find_definition.return_value = []

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {
                "symbol": "UserService",
                "exact": True,
                "repository_alias": "my-repo",
            }
            scip_definition(params, mock_user)

            mock_service.find_definition.assert_called_once_with(
                symbol="UserService",
                exact=True,
                repository_alias="my-repo",
                username="testuser",
            )

    def test_scip_definition_passes_username_for_access_control(
        self, mock_user: User
    ) -> None:
        """AC: Verify username is passed to service for access control filtering."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = MagicMock()
        mock_service.find_definition.return_value = []

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            scip_definition({"symbol": "Symbol"}, mock_user)

            call_kwargs = mock_service.find_definition.call_args[1]
            assert call_kwargs["username"] == "testuser"

    def test_scip_definition_returns_error_for_missing_symbol(
        self, mock_user: User
    ) -> None:
        """AC: Verify error response for missing symbol parameter."""
        from code_indexer.server.mcp.handlers import scip_definition

        response = scip_definition({}, mock_user)

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "symbol" in data["error"].lower()
        assert "required" in data["error"].lower()

    def test_scip_definition_catches_service_exceptions(
        self, mock_user: User
    ) -> None:
        """Verify handler catches and returns errors when service raises exception."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = MagicMock()
        mock_service.find_definition.side_effect = Exception("Database error")

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_definition({"symbol": "test"}, mock_user)

            data = json.loads(response["content"][0]["text"])
            assert data["success"] is False
            assert "Database error" in data["error"]
