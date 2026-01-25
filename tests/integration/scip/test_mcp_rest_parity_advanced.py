"""
MCP/REST SCIP Parity Tests - Advanced Operations (Story #42)

Tests parity for: scip_impact, scip_callchain, scip_context.

Acceptance Criteria (Story #42):
- MCP and REST return identical results for all 7 SCIP operations
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


class TestSCIPImpactParity:
    """Tests for scip_impact MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_impact(
        self, mock_user, mock_scip_query_service
    ):
        """MCP and REST return identical results for scip_impact."""
        from code_indexer.server.mcp.handlers import scip_impact
        from code_indexer.server.routers.scip_queries import get_impact

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_impact(
                {"symbol": "PaymentProcessor", "depth": 3}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_impact(
                request=mock_request,
                symbol="PaymentProcessor",
                depth=3,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["target_symbol"] == rest_response["target_symbol"]
        assert mcp_data["depth_analyzed"] == rest_response["depth_analyzed"]
        assert mcp_data["total_affected"] == rest_response["total_affected"]
        assert mcp_data["affected_symbols"] == rest_response["affected_symbols"]
        assert mcp_data["affected_files"] == rest_response["affected_files"]


class TestSCIPCallchainParity:
    """Tests for scip_callchain MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_callchain(
        self, mock_user, mock_scip_query_service
    ):
        """MCP and REST return identical results for scip_callchain."""
        from code_indexer.server.mcp.handlers import scip_callchain
        from code_indexer.server.routers.scip_queries import get_callchain

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_callchain(
                {
                    "from_symbol": "handleRequest",
                    "to_symbol": "sanitize",
                    "max_depth": 10,
                },
                mock_user,
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_callchain(
                request=mock_request,
                from_symbol="handleRequest",
                to_symbol="sanitize",
                max_depth=10,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["from_symbol"] == rest_response["from_symbol"]
        assert mcp_data["to_symbol"] == rest_response["to_symbol"]
        assert mcp_data["chains"] == rest_response["chains"]


class TestSCIPContextParity:
    """Tests for scip_context MCP/REST parity."""

    @pytest.mark.asyncio
    async def test_mcp_and_rest_return_identical_results_for_context(
        self, mock_user, mock_scip_query_service
    ):
        """MCP and REST return identical results for scip_context."""
        from code_indexer.server.mcp.handlers import scip_context
        from code_indexer.server.routers.scip_queries import get_context

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            mcp_response = await scip_context(
                {"symbol": "UserService", "limit": 20, "min_score": 0.0}, mock_user
            )

        mcp_data = _extract_mcp_response_data(mcp_response)

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_query_service,
        ):
            rest_response = await get_context(
                request=mock_request,
                symbol="UserService",
                limit=20,
                min_score=0.0,
                project=None,
                current_user=mock_user,
            )

        assert mcp_data["success"] == rest_response["success"]
        assert mcp_data["target_symbol"] == rest_response["target_symbol"]
        assert mcp_data["total_files"] == rest_response["total_files"]
        assert mcp_data["total_symbols"] == rest_response["total_symbols"]
        assert mcp_data["files"] == rest_response["files"]
