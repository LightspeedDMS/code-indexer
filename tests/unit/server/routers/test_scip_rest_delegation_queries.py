"""
Unit tests for REST SCIP query route delegation to SCIPQueryService.

Story #41: Refactor REST SCIP Routes to Use SCIPQueryService

Tests for definition, references, dependencies, and dependents endpoints.
Following TDD methodology - these tests are written FIRST before implementation.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
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


class TestGetScipQueryServiceHelper:
    """Tests for the _get_scip_query_service helper function."""

    def test_creates_service_with_golden_repos_dir_from_app_state(self):
        """Verify helper gets golden_repos_dir from request.app.state."""
        from code_indexer.server.routers.scip_queries import _get_scip_query_service

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/path/to/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries.SCIPQueryService"
        ) as MockService:
            _get_scip_query_service(mock_request)

            MockService.assert_called_once_with(
                golden_repos_dir="/path/to/golden-repos",
                access_filtering_service=None,
            )

    def test_creates_service_with_access_filtering_service(self):
        """Verify helper passes access_filtering_service to SCIPQueryService."""
        from code_indexer.server.routers.scip_queries import _get_scip_query_service

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/path/to/golden-repos"
        mock_access_service = MagicMock()
        mock_request.app.state.access_filtering_service = mock_access_service

        with patch(
            "code_indexer.server.routers.scip_queries.SCIPQueryService"
        ) as MockService:
            _get_scip_query_service(mock_request)

            MockService.assert_called_once_with(
                golden_repos_dir="/path/to/golden-repos",
                access_filtering_service=mock_access_service,
            )


class TestDefinitionRouteDelegation:
    """Tests for /scip/definition route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_definition_route_calls_service_find_definition(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/definition route delegates to SCIPQueryService.find_definition()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_definition

        mock_scip_service.find_definition.return_value = [
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

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
                new_callable=AsyncMock,
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.find_definition.return_value
                )
                response = await get_definition(
                    request=mock_request,
                    symbol="UserService",
                    exact=False,
                    project=None,
                    current_user=mock_user,
                )

                # Verify service method was called with correct parameters
                mock_scip_service.find_definition.assert_called_once_with(
                    symbol="UserService",
                    exact=False,
                    repository_alias=None,
                    username="testuser",
                )

                # Verify REST response format
                assert response["success"] is True
                assert response["symbol"] == "UserService"
                assert response["total_results"] == 1
                assert len(response["results"]) == 1

    @pytest.mark.asyncio
    async def test_definition_route_passes_project_as_repository_alias(
        self, mock_user, mock_scip_service
    ):
        """Verify project parameter is passed as repository_alias to service."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_definition

        mock_scip_service.find_definition.return_value = []
        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
                new_callable=AsyncMock,
            ) as mock_truncate:
                mock_truncate.return_value = []
                await get_definition(
                    request=mock_request,
                    symbol="UserService",
                    exact=True,
                    project="my-repo",
                    current_user=mock_user,
                )

                mock_scip_service.find_definition.assert_called_once_with(
                    symbol="UserService",
                    exact=True,
                    repository_alias="my-repo",
                    username="testuser",
                )


class TestReferencesRouteDelegation:
    """Tests for /scip/references route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_references_route_calls_service_find_references(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/references route delegates to SCIPQueryService.find_references()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_references

        mock_scip_service.find_references.return_value = [
            {
                "symbol": "UserService",
                "project": "repo-a",
                "file_path": "src/handler.py",
                "line": 20,
                "column": 10,
                "kind": "reference",
                "relationship": "call",
                "context": "user_svc = UserService()",
            }
        ]

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
                new_callable=AsyncMock,
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.find_references.return_value
                )
                response = await get_references(
                    request=mock_request,
                    symbol="UserService",
                    limit=50,
                    exact=False,
                    project=None,
                    current_user=mock_user,
                )

                mock_scip_service.find_references.assert_called_once_with(
                    symbol="UserService",
                    limit=50,
                    exact=False,
                    repository_alias=None,
                    username="testuser",
                )

                assert response["success"] is True
                assert response["symbol"] == "UserService"
                assert response["total_results"] == 1


class TestDependenciesRouteDelegation:
    """Tests for /scip/dependencies route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_dependencies_route_calls_service_get_dependencies(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/dependencies route delegates to get_dependencies()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_dependencies

        mock_scip_service.get_dependencies.return_value = [
            {
                "symbol": "Database",
                "project": "repo-a",
                "file_path": "src/db.py",
                "line": 10,
                "column": 0,
                "kind": "dependency",
                "relationship": "import",
                "context": None,
            }
        ]

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
                new_callable=AsyncMock,
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.get_dependencies.return_value
                )
                response = await get_dependencies(
                    request=mock_request,
                    symbol="UserService",
                    depth=2,
                    exact=False,
                    project=None,
                    current_user=mock_user,
                )

                mock_scip_service.get_dependencies.assert_called_once_with(
                    symbol="UserService",
                    depth=2,
                    exact=False,
                    repository_alias=None,
                    username="testuser",
                )

                assert response["success"] is True
                assert response["symbol"] == "UserService"


class TestDependentsRouteDelegation:
    """Tests for /scip/dependents route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_dependents_route_calls_service_get_dependents(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/dependents route delegates to SCIPQueryService.get_dependents()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_dependents

        mock_scip_service.get_dependents.return_value = [
            {
                "symbol": "AuthHandler",
                "project": "repo-a",
                "file_path": "src/auth.py",
                "line": 30,
                "column": 5,
                "kind": "dependent",
                "relationship": "call",
                "context": None,
            }
        ]

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
                new_callable=AsyncMock,
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.get_dependents.return_value
                )
                response = await get_dependents(
                    request=mock_request,
                    symbol="UserService",
                    depth=1,
                    exact=True,
                    project="my-repo",
                    current_user=mock_user,
                )

                mock_scip_service.get_dependents.assert_called_once_with(
                    symbol="UserService",
                    depth=1,
                    exact=True,
                    repository_alias="my-repo",
                    username="testuser",
                )

                assert response["success"] is True
