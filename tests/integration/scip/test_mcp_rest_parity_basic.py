"""
MCP/REST SCIP Parity Tests - Basic Operations (Story #42)

Tests parity for: scip_definition, scip_references, scip_dependencies, scip_dependents.

Acceptance Criteria (Story #42):
- MCP and REST return identical results for scip_definition
- MCP and REST return identical results for scip_references
- Both interfaces call SCIPQueryService internally
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from typing import Any, Dict


def _extract_mcp_response_data(mcp_response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract data from MCP-compliant response structure."""
    assert "content" in mcp_response, "MCP response missing 'content' key"
    assert len(mcp_response["content"]) >= 1, "MCP response content array is empty"
    assert mcp_response["content"][0]["type"] == "text", "MCP response not 'text'"
    return json.loads(mcp_response["content"][0]["text"])


class TestSCIPDefinitionParity:
    """Tests for scip_definition MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_definition(
        self, mock_user, mock_scip_query_service
    ):
        """
        Acceptance Criteria: MCP and REST return identical results for scip_definition.

        Given a SCIP index with symbol "UserService" defined at line 45
        When scip_definition is called via MCP with symbol="UserService"
        And /scip/definition is called via REST with symbol="UserService"
        Then both responses have identical "results" arrays
        And both responses have identical "count" values
        """
        from code_indexer.server.mcp.handlers import scip_definition
        from code_indexer.server.routers.scip_queries import get_definition

        # Test MCP handler
        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_definition(
                {"symbol": "UserService", "exact": False}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        # Test REST endpoint
        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_definition(
                request=mock_request,
                symbol="UserService",
                exact=False,
                project=None,
                current_user=mock_user,
            )

        # Verify parity
        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["symbol"] == rest_response["symbol"]
        assert mcp_data["total_results"] == rest_response["total_results"]
        assert mcp_data["results"] == rest_response["results"]


class TestSCIPReferencesParity:
    """Tests for scip_references MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_references(
        self, mock_user, mock_scip_query_service
    ):
        """
        Acceptance Criteria: MCP and REST return identical results for scip_references.

        Given a SCIP index with symbol "authenticate" referenced multiple times
        When scip_references is called via MCP
        And /scip/references is called via REST
        Then both responses contain the same references
        """
        from code_indexer.server.mcp.handlers import scip_references
        from code_indexer.server.routers.scip_queries import get_references

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_references(
                {"symbol": "authenticate", "limit": 100, "exact": False}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_references(
                request=mock_request,
                symbol="authenticate",
                limit=100,
                exact=False,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["total_results"] == rest_response["total_results"]
        assert len(mcp_data["results"]) == len(rest_response["results"])
        assert mcp_data["results"] == rest_response["results"]


class TestSCIPDependenciesParity:
    """Tests for scip_dependencies MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_dependencies(
        self, mock_user, mock_scip_query_service
    ):
        """MCP and REST return identical results for scip_dependencies."""
        from code_indexer.server.mcp.handlers import scip_dependencies
        from code_indexer.server.routers.scip_queries import get_dependencies

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_dependencies(
                {"symbol": "UserService", "depth": 1, "exact": False}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_dependencies(
                request=mock_request,
                symbol="UserService",
                depth=1,
                exact=False,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["total_results"] == rest_response["total_results"]
        assert mcp_data["results"] == rest_response["results"]


class TestSCIPDependentsParity:
    """Tests for scip_dependents MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_dependents(
        self, mock_user, mock_scip_query_service
    ):
        """MCP and REST return identical results for scip_dependents."""
        from code_indexer.server.mcp.handlers import scip_dependents
        from code_indexer.server.routers.scip_queries import get_dependents

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_dependents(
                {"symbol": "PaymentProcessor", "depth": 1, "exact": False}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_dependents(
                request=mock_request,
                symbol="PaymentProcessor",
                depth=1,
                exact=False,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["total_results"] == rest_response["total_results"]
        assert mcp_data["results"] == rest_response["results"]
