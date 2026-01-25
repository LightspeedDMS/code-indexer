"""
SCIP Service Delegation Tests - Story #42

Tests verifying both MCP and REST delegate to SCIPQueryService.

Acceptance Criteria (Story #42):
- No duplicate _find_scip_files implementations exist
- Both interfaces call SCIPQueryService internally
- All SCIP operations work end-to-end
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from typing import Any, Dict


def _extract_mcp_response_data(mcp_response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract data from MCP-compliant response structure."""
    assert "content" in mcp_response, "MCP response missing 'content' key"
    assert len(mcp_response["content"]) >= 1, "MCP response content array is empty"
    assert mcp_response["content"][0]["type"] == "text", "MCP response not 'text'"
    return json.loads(mcp_response["content"][0]["text"])


class TestSCIPServiceDelegation:
    """Tests verifying both MCP and REST delegate to SCIPQueryService."""

    @pytest.mark.asyncio
    async def test_mcp_handler_delegates_to_scip_query_service(self, mock_user):
        """Verify MCP handlers call SCIPQueryService methods."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = Mock()
        mock_service.find_definition.return_value = []

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ) as mock_get_service:
            await scip_definition({"symbol": "TestSymbol"}, mock_user)

            mock_get_service.assert_called_once()
            mock_service.find_definition.assert_called_once_with(
                symbol="TestSymbol",
                exact=False,
                repository_alias=None,
                username="testuser",
            )

    @pytest.mark.asyncio
    async def test_rest_handler_delegates_to_scip_query_service(self, mock_user):
        """Verify REST handlers call SCIPQueryService methods."""
        from code_indexer.server.routers.scip_queries import get_definition

        mock_service = Mock()
        mock_service.find_definition.return_value = []

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_service,
        ) as mock_get_service:
            await get_definition(
                request=mock_request,
                symbol="TestSymbol",
                exact=False,
                project=None,
                current_user=mock_user,
            )

            mock_get_service.assert_called_once_with(mock_request)
            mock_service.find_definition.assert_called_once_with(
                symbol="TestSymbol",
                exact=False,
                repository_alias=None,
                username="testuser",
            )

    def test_both_interfaces_have_scip_query_service_getter(self):
        """Verify both MCP and REST have _get_scip_query_service function."""
        from code_indexer.server.mcp import handlers as mcp_handlers
        from code_indexer.server.routers import scip_queries as rest_router

        assert hasattr(mcp_handlers, "_get_scip_query_service"), (
            "MCP handlers must have _get_scip_query_service function"
        )
        assert hasattr(rest_router, "_get_scip_query_service"), (
            "REST router must have _get_scip_query_service function"
        )

    def test_rest_router_imports_scip_query_service(self):
        """Verify REST router imports SCIPQueryService directly."""
        from code_indexer.server.routers import scip_queries as rest_router

        assert hasattr(rest_router, "SCIPQueryService") or (
            "SCIPQueryService" in dir(rest_router)
        ), "REST router must import SCIPQueryService"

    def test_mcp_handler_references_scip_query_service(self):
        """Verify MCP handler references SCIPQueryService."""
        from code_indexer.server.mcp import handlers as mcp_handlers

        mcp_uses_scip_service = (
            "SCIPQueryService" in mcp_handlers._get_scip_query_service.__code__.co_names
        )
        assert mcp_uses_scip_service, (
            "MCP handlers must reference SCIPQueryService in _get_scip_query_service"
        )


class TestAllSCIPOperationsMCP:
    """End-to-end tests for all 7 SCIP operations via MCP."""

    @pytest.mark.asyncio
    async def test_all_mcp_operations_complete_successfully(
        self, mock_user, mock_scip_query_service
    ):
        """All 7 SCIP operations complete successfully via MCP."""
        from code_indexer.server.mcp.handlers import (
            scip_definition,
            scip_references,
            scip_dependencies,
            scip_dependents,
            scip_impact,
            scip_callchain,
            scip_context,
        )

        mcp_operations = [
            (scip_definition, {"symbol": "TestSymbol"}),
            (scip_references, {"symbol": "TestSymbol", "limit": 100}),
            (scip_dependencies, {"symbol": "TestSymbol", "depth": 1}),
            (scip_dependents, {"symbol": "TestSymbol", "depth": 1}),
            (scip_impact, {"symbol": "TestSymbol", "depth": 3}),
            (scip_callchain, {"from_symbol": "A", "to_symbol": "B", "max_depth": 10}),
            (scip_context, {"symbol": "TestSymbol", "limit": 20, "min_score": 0.0}),
        ]

        for handler, params in mcp_operations:
            with patch(
                "code_indexer.server.mcp.handlers._get_scip_query_service",
                return_value=mock_scip_query_service,
            ):
                mcp_response = await handler(params, mock_user)

            mcp_data = _extract_mcp_response_data(mcp_response)
            assert mcp_data["success"] is True, (
                f"MCP {handler.__name__} failed: {mcp_data.get('error')}"
            )


class TestAllSCIPOperationsREST:
    """End-to-end tests for all 7 SCIP operations via REST."""

    @pytest.mark.asyncio
    async def test_all_rest_operations_complete_successfully(
        self, mock_user, mock_scip_query_service
    ):
        """All 7 SCIP operations complete successfully via REST."""
        from code_indexer.server.routers.scip_queries import (
            get_definition,
            get_references,
            get_dependencies,
            get_dependents,
            get_impact,
            get_callchain,
            get_context,
        )

        mock_request = MagicMock()
        mock_request.app.state.golden_repos_dir = "/test/golden-repos"
        mock_request.app.state.access_filtering_service = None

        rest_operations = [
            (get_definition, {"symbol": "TestSymbol", "exact": False, "project": None}),
            (get_references, {
                "symbol": "TestSymbol", "limit": 100, "exact": False, "project": None
            }),
            (get_dependencies, {
                "symbol": "TestSymbol", "depth": 1, "exact": False, "project": None
            }),
            (get_dependents, {
                "symbol": "TestSymbol", "depth": 1, "exact": False, "project": None
            }),
            (get_impact, {"symbol": "TestSymbol", "depth": 3, "project": None}),
            (get_callchain, {
                "from_symbol": "A", "to_symbol": "B", "max_depth": 10, "project": None
            }),
            (get_context, {
                "symbol": "TestSymbol", "limit": 20, "min_score": 0.0, "project": None
            }),
        ]

        for handler, kwargs in rest_operations:
            with patch(
                "code_indexer.server.routers.scip_queries._get_scip_query_service",
                return_value=mock_scip_query_service,
            ):
                rest_response = await handler(
                    request=mock_request,
                    current_user=mock_user,
                    **kwargs,
                )

            assert rest_response["success"] is True, (
                f"REST {handler.__name__} failed: {rest_response.get('error')}"
            )
