"""
Tests for SCIPAPIClient - SCIP Code Intelligence from CLI Remote Mode (Story #736).

Following TDD methodology: Writing failing tests first to define expected behavior.
Tests the SCIPAPIClient class for remote SCIP operations via REST API.
Uses mock HTTP responses to test client logic without requiring real server.
"""

import pytest
from unittest.mock import AsyncMock, patch
from typing import Dict, Any
from pathlib import Path
import tempfile

from code_indexer.api_clients.scip_client import (
    SCIPAPIClient,
    SCIPQueryError,
    SCIPNotFoundError,
)


# Module-level fixtures to avoid DRY violations
@pytest.fixture
def admin_credentials() -> Dict[str, Any]:
    """Admin credentials for testing."""
    return {"username": "admin", "password": "admin123"}


@pytest.fixture
def temp_project_root():
    """Create temporary project root for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def scip_client(admin_credentials, temp_project_root):
    """Create SCIP API client for testing."""
    return SCIPAPIClient(
        server_url="https://test.example.com",
        credentials=admin_credentials,
        project_root=temp_project_root,
    )


class TestSCIPAPIClientInitialization:
    """Test SCIPAPIClient initialization."""

    def test_client_initialization(self, admin_credentials, temp_project_root):
        """Test SCIPAPIClient initialization with all parameters."""
        client = SCIPAPIClient(
            server_url="https://test.example.com",
            credentials=admin_credentials,
            project_root=temp_project_root,
        )

        assert client.server_url == "https://test.example.com"
        assert client.credentials == admin_credentials
        assert client.project_root == temp_project_root

    def test_client_initialization_without_project_root(self, admin_credentials):
        """Test SCIPAPIClient initialization without project root."""
        client = SCIPAPIClient(
            server_url="https://test.example.com",
            credentials=admin_credentials,
            project_root=None,
        )

        assert client.server_url == "https://test.example.com"
        assert client.credentials == admin_credentials
        assert client.project_root is None


class TestSCIPAPIClientDefinition:
    """Test SCIPAPIClient.definition() method."""

    @pytest.mark.asyncio
    async def test_definition_returns_dict_response(self, scip_client):
        """Test that definition() returns a dictionary response."""
        mock_response = {
            "results": {"test-repo": [{"symbol": "TestSymbol", "file": "test.py"}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(status_code=200, json=lambda: mock_response),
        ):
            result = await scip_client.definition(
                symbol="TestSymbol",
                repository_alias="test-repo",
            )
            assert isinstance(result, dict)
            assert "results" in result

    @pytest.mark.asyncio
    async def test_definition_passes_project_filter(self, scip_client):
        """Test definition() passes project filter to request."""
        mock_response = {
            "results": {"test-repo": []},
            "metadata": {"total_results": 0},
            "errors": {},
        }

        mock_request = AsyncMock(
            return_value=AsyncMock(status_code=200, json=lambda: mock_response)
        )
        with patch.object(scip_client, "_authenticated_request", mock_request):
            await scip_client.definition(
                symbol="FilteredSymbol",
                repository_alias="test-repo",
                project="backend/",
            )
            # Verify the request was made with correct endpoint and payload
            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/scip/multi/definition" in call_args[0][1]
            payload = call_args[1]["json"]
            assert payload["symbol"] == "FilteredSymbol"
            assert payload["project"] == "backend/"


class TestSCIPAPIClientReferences:
    """Test SCIPAPIClient.references() method."""

    @pytest.mark.asyncio
    async def test_references_returns_dict_response(self, scip_client):
        """Test references() returns a dictionary response."""
        mock_response = {
            "results": {"test-repo": [{"symbol": "TestClass", "file": "test.py"}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(status_code=200, json=lambda: mock_response),
        ):
            result = await scip_client.references(
                symbol="TestClass",
                repository_alias="test-repo",
                limit=50,
            )
            assert isinstance(result, dict)
            assert "results" in result

    @pytest.mark.asyncio
    async def test_references_passes_limit_parameter(self, scip_client):
        """Test references() passes limit parameter to request."""
        mock_response = {
            "results": {"test-repo": []},
            "metadata": {"total_results": 0},
            "errors": {},
        }

        mock_request = AsyncMock(
            return_value=AsyncMock(status_code=200, json=lambda: mock_response)
        )
        with patch.object(scip_client, "_authenticated_request", mock_request):
            await scip_client.references(
                symbol="MyClass",
                repository_alias="backend-global",
                limit=100,
            )
            mock_request.assert_called_once()
            call_args = mock_request.call_args
            payload = call_args[1]["json"]
            assert payload["limit"] == 100
            assert payload["repositories"] == ["backend-global"]


class TestSCIPAPIClientDependencies:
    """Test SCIPAPIClient.dependencies() method."""

    @pytest.mark.asyncio
    async def test_dependencies_returns_dict_response(self, scip_client):
        """Test dependencies() returns a dictionary response."""
        mock_response = {
            "results": {"test-repo": [{"symbol": "MyService", "deps": ["Logger"]}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(status_code=200, json=lambda: mock_response),
        ):
            result = await scip_client.dependencies(
                symbol="MyService",
                repository_alias="test-repo",
                depth=2,
            )
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_dependencies_passes_depth_as_max_depth(self, scip_client):
        """Test dependencies() passes depth parameter as max_depth."""
        mock_response = {"results": {}, "metadata": {}, "errors": {}}

        mock_request = AsyncMock(
            return_value=AsyncMock(status_code=200, json=lambda: mock_response)
        )
        with patch.object(scip_client, "_authenticated_request", mock_request):
            await scip_client.dependencies(
                symbol="MyService",
                repository_alias="test-repo",
                depth=3,
            )
            mock_request.assert_called_once()
            payload = mock_request.call_args[1]["json"]
            assert payload["max_depth"] == 3


class TestSCIPAPIClientDependents:
    """Test SCIPAPIClient.dependents() method."""

    @pytest.mark.asyncio
    async def test_dependents_returns_dict_with_depth(self, scip_client):
        """Test dependents() returns dict response and passes depth parameter."""
        mock_response = {
            "results": {"test-repo": [{"symbol": "Logger", "dependents": ["App"]}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        mock_request = AsyncMock(
            return_value=AsyncMock(status_code=200, json=lambda: mock_response)
        )
        with patch.object(scip_client, "_authenticated_request", mock_request):
            result = await scip_client.dependents(
                symbol="Logger",
                repository_alias="test-repo",
                depth=2,
            )
            assert isinstance(result, dict)
            payload = mock_request.call_args[1]["json"]
            assert payload["max_depth"] == 2


class TestSCIPAPIClientImpact:
    """Test SCIPAPIClient.impact() method."""

    @pytest.mark.asyncio
    async def test_impact_caps_depth_at_10(self, scip_client):
        """Test impact() caps depth at 10 even when higher value passed."""
        mock_response = {"results": {}, "metadata": {}, "errors": {}}

        mock_request = AsyncMock(
            return_value=AsyncMock(status_code=200, json=lambda: mock_response)
        )
        with patch.object(scip_client, "_authenticated_request", mock_request):
            result = await scip_client.impact(
                symbol="Config",
                repository_alias="test-repo",
                depth=15,  # Should be capped to 10
            )
            assert isinstance(result, dict)
            payload = mock_request.call_args[1]["json"]
            assert payload["max_depth"] == 10  # Capped at 10


class TestSCIPAPIClientCallchain:
    """Test SCIPAPIClient.callchain() method."""

    @pytest.mark.asyncio
    async def test_callchain_returns_dict_response(self, scip_client):
        """Test callchain() returns dict response with both symbols."""
        mock_response = {
            "results": {"test-repo": [{"chain": ["main", "process"]}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(status_code=200, json=lambda: mock_response),
        ):
            result = await scip_client.callchain(
                from_symbol="main",
                to_symbol="process_request",
                repository_alias="test-repo",
                max_depth=10,
            )
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_callchain_validates_empty_from_symbol(self, scip_client):
        """Test callchain() raises ValueError for empty from_symbol."""
        with pytest.raises(ValueError, match="from_symbol cannot be empty"):
            await scip_client.callchain(
                from_symbol="",
                to_symbol="end",
                repository_alias="test-repo",
            )

    @pytest.mark.asyncio
    async def test_callchain_validates_empty_to_symbol(self, scip_client):
        """Test callchain() raises ValueError for empty to_symbol."""
        with pytest.raises(ValueError, match="to_symbol cannot be empty"):
            await scip_client.callchain(
                from_symbol="start",
                to_symbol="",
                repository_alias="test-repo",
            )


class TestSCIPAPIClientContext:
    """Test SCIPAPIClient.context() method."""

    @pytest.mark.asyncio
    async def test_context_returns_combined_results(self, scip_client):
        """Test context() returns combined definition and references."""
        mock_def_response = {
            "results": {"test-repo": [{"symbol": "MyService", "file": "svc.py"}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }
        mock_ref_response = {
            "results": {"test-repo": [{"symbol": "MyService", "file": "app.py"}]},
            "metadata": {"total_results": 1},
            "errors": {},
        }

        call_count = [0]

        def mock_request_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return AsyncMock(status_code=200, json=lambda: mock_def_response)
            return AsyncMock(status_code=200, json=lambda: mock_ref_response)

        with patch.object(
            scip_client, "_authenticated_request", side_effect=mock_request_side_effect
        ):
            result = await scip_client.context(
                symbol="MyService",
                repository_alias="test-repo",
                limit=10,
            )
            assert isinstance(result, dict)
            assert "results" in result

    @pytest.mark.asyncio
    async def test_context_validates_limit_minimum(self, scip_client):
        """Test context() validates limit is at least 1."""
        with pytest.raises(ValueError, match="limit must be at least 1"):
            await scip_client.context(
                symbol="MyService",
                repository_alias="test-repo",
                limit=0,
            )


class TestSCIPAPIClientErrorHandling:
    """Test SCIPAPIClient error handling."""

    @pytest.mark.asyncio
    async def test_404_raises_scip_not_found_error(self, scip_client):
        """Test that 404 response raises SCIPNotFoundError."""
        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(
                status_code=404,
                json=lambda: {"detail": "Symbol not found"},
            ),
        ):
            with pytest.raises(SCIPNotFoundError):
                await scip_client.definition(
                    symbol="NonExistent",
                    repository_alias="test-repo",
                )

    @pytest.mark.asyncio
    async def test_500_raises_scip_query_error(self, scip_client):
        """Test that 500 response raises SCIPQueryError."""
        with patch.object(
            scip_client,
            "_authenticated_request",
            return_value=AsyncMock(
                status_code=500,
                json=lambda: {"detail": "Internal server error"},
            ),
        ):
            with pytest.raises(SCIPQueryError):
                await scip_client.definition(
                    symbol="TestSymbol",
                    repository_alias="test-repo",
                )
