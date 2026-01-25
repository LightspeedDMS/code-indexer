"""
Unit tests for REST SCIP analysis route delegation to SCIPQueryService.

Story #41: Refactor REST SCIP Routes to Use SCIPQueryService

Tests for impact, callchain, and context endpoints.
Following TDD methodology - these tests are written FIRST before implementation.
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


class TestImpactRouteDelegation:
    """Tests for /scip/impact route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_impact_route_calls_service_analyze_impact(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/impact route delegates to SCIPQueryService.analyze_impact()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_impact

        mock_scip_service.analyze_impact.return_value = {
            "target_symbol": "UserService",
            "depth_analyzed": 3,
            "total_affected": 2,
            "truncated": False,
            "affected_symbols": [
                {
                    "symbol": "AuthHandler",
                    "file_path": "src/auth.py",
                    "line": 20,
                    "column": 5,
                    "depth": 1,
                    "relationship": "call",
                    "chain": ["UserService", "AuthHandler"],
                }
            ],
            "affected_files": [
                {
                    "path": "src/auth.py",
                    "project": "repo-a",
                    "affected_symbol_count": 1,
                    "min_depth": 1,
                    "max_depth": 1,
                }
            ],
        }

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = await get_impact(
                request=mock_request,
                symbol="UserService",
                depth=3,
                project=None,
                current_user=mock_user,
            )

            mock_scip_service.analyze_impact.assert_called_once_with(
                symbol="UserService",
                depth=3,
                repository_alias=None,
                username="testuser",
            )

            assert response["success"] is True
            assert response["target_symbol"] == "UserService"
            assert response["depth_analyzed"] == 3
            assert "affected_symbols" in response
            assert "affected_files" in response

    @pytest.mark.asyncio
    async def test_impact_route_passes_project_as_repository_alias(
        self, mock_user, mock_scip_service
    ):
        """Verify project parameter is passed as repository_alias to service."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_impact

        mock_scip_service.analyze_impact.return_value = {
            "target_symbol": "UserService",
            "depth_analyzed": 3,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            await get_impact(
                request=mock_request,
                symbol="UserService",
                depth=5,
                project="my-repo",
                current_user=mock_user,
            )

            mock_scip_service.analyze_impact.assert_called_once_with(
                symbol="UserService",
                depth=5,
                repository_alias="my-repo",
                username="testuser",
            )


class TestCallchainRouteDelegation:
    """Tests for /scip/callchain route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_callchain_route_calls_service_trace_callchain(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/callchain route delegates to SCIPQueryService.trace_callchain()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_callchain

        mock_scip_service.trace_callchain.return_value = [
            {
                "path": ["Controller.handle", "Service.process", "Database.query"],
                "length": 3,
                "has_cycle": False,
            }
        ]

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = await get_callchain(
                request=mock_request,
                from_symbol="Controller",
                to_symbol="Database",
                max_depth=10,
                project=None,
                current_user=mock_user,
            )

            mock_scip_service.trace_callchain.assert_called_once_with(
                from_symbol="Controller",
                to_symbol="Database",
                max_depth=10,
                repository_alias=None,
                username="testuser",
            )

            assert response["success"] is True
            assert response["from_symbol"] == "Controller"
            assert response["to_symbol"] == "Database"
            assert "chains" in response

    @pytest.mark.asyncio
    async def test_callchain_route_passes_project_as_repository_alias(
        self, mock_user, mock_scip_service
    ):
        """Verify project parameter is passed as repository_alias to service."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_callchain

        mock_scip_service.trace_callchain.return_value = []

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            await get_callchain(
                request=mock_request,
                from_symbol="A",
                to_symbol="B",
                max_depth=15,
                project="my-repo",
                current_user=mock_user,
            )

            mock_scip_service.trace_callchain.assert_called_once_with(
                from_symbol="A",
                to_symbol="B",
                max_depth=15,
                repository_alias="my-repo",
                username="testuser",
            )


class TestContextRouteDelegation:
    """Tests for /scip/context route delegation to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_context_route_calls_service_get_context(
        self, mock_user, mock_scip_service
    ):
        """AC: /scip/context route delegates to SCIPQueryService.get_context()."""
        from fastapi import Request
        from code_indexer.server.routers.scip_queries import get_context

        mock_scip_service.get_context.return_value = {
            "target_symbol": "UserService",
            "summary": "Read these 1 file(s) to understand UserService",
            "files": [
                {
                    "path": "src/services.py",
                    "project": "repo-a",
                    "relevance_score": 0.9,
                    "read_priority": 1,
                    "symbols": [
                        {
                            "name": "UserService",
                            "kind": "class",
                            "relationship": "definition",
                            "line": 10,
                            "column": 0,
                            "relevance": 1.0,
                        }
                    ],
                }
            ],
            "total_files": 1,
            "total_symbols": 1,
            "avg_relevance": 0.9,
        }

        mock_request = MagicMock(spec=Request)

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = await get_context(
                request=mock_request,
                symbol="UserService",
                limit=20,
                min_score=0.5,
                project=None,
                current_user=mock_user,
            )

            mock_scip_service.get_context.assert_called_once_with(
                symbol="UserService",
                limit=20,
                min_score=0.5,
                repository_alias=None,
                username="testuser",
            )

            assert response["success"] is True
            assert response["target_symbol"] == "UserService"
            assert "files" in response
            assert response["total_files"] == 1

    @pytest.mark.asyncio
    async def test_context_route_passes_project_as_repository_alias(
        self, mock_user, mock_scip_service
    ):
        """Verify project parameter is passed as repository_alias to service."""
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
            await get_context(
                request=mock_request,
                symbol="UserService",
                limit=30,
                min_score=0.7,
                project="my-repo",
                current_user=mock_user,
            )

            mock_scip_service.get_context.assert_called_once_with(
                symbol="UserService",
                limit=30,
                min_score=0.7,
                repository_alias="my-repo",
                username="testuser",
            )
