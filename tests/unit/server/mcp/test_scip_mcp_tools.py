"""Unit tests for SCIP MCP tool handlers."""

import pytest
from unittest.mock import Mock, patch
import json
from datetime import datetime, timezone

from code_indexer.scip.query.primitives import QueryResult
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
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
def mock_scip_files(tmp_path):
    """Create mock SCIP file paths for testing."""
    scip_dir = tmp_path / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    scip_file = scip_dir / "project1.scip"
    scip_file.touch()
    return [scip_file]


class TestSCIPDefinitionTool:
    """Tests for scip_definition MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_definition_returns_mcp_response(
        self, mock_user, mock_scip_files
    ):
        """Should return MCP-compliant response with definition results."""
        from code_indexer.server.mcp.handlers import scip_definition

        params = {"symbol": "UserService", "exact": False}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.find_definition.return_value = [
            {
                "symbol": "com.example.UserService",
                "project": "/path/to/project1",
                "file_path": "src/services/user_service.py",
                "line": 10,
                "column": 5,
                "kind": "definition",
                "relationship": None,
                "context": None,
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_definition(params, mock_user)

            # Verify MCP-compliant response structure
            assert "content" in response
            assert len(response["content"]) == 1
            assert response["content"][0]["type"] == "text"

            # Parse JSON response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["symbol"] == "UserService"
            assert data["total_results"] >= 1
            assert len(data["results"]) >= 1
            assert data["results"][0]["kind"] == "definition"


class TestSCIPReferencesTool:
    """Tests for scip_references MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_references_returns_mcp_response(
        self, mock_user, mock_scip_files
    ):
        """Should return MCP-compliant response with reference results."""
        from code_indexer.server.mcp.handlers import scip_references

        params = {"symbol": "UserService", "limit": 100, "exact": False}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.find_references.return_value = [
            {
                "symbol": "com.example.UserService",
                "project": "/path/to/project1",
                "file_path": "src/auth/handler.py",
                "line": 15,
                "column": 10,
                "kind": "reference",
                "relationship": "call",
                "context": None,
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_references(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["results"][0]["kind"] == "reference"


class TestSCIPDependenciesTool:
    """Tests for scip_dependencies MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_dependencies_returns_mcp_response(
        self, mock_user, mock_scip_files
    ):
        """Should return MCP-compliant response with dependency results."""
        from code_indexer.server.mcp.handlers import scip_dependencies

        params = {"symbol": "UserService", "exact": False}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.get_dependencies.return_value = [
            {
                "symbol": "com.example.Database",
                "project": "/path/to/project1",
                "file_path": "src/services/user_service.py",
                "line": 5,
                "column": 0,
                "kind": "dependency",
                "relationship": "import",
                "context": None,
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_dependencies(params, mock_user)

            # Verify MCP-compliant response structure
            assert "content" in response
            assert len(response["content"]) == 1
            assert response["content"][0]["type"] == "text"

            # Parse JSON response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["symbol"] == "UserService"
            assert data["total_results"] >= 1
            assert len(data["results"]) >= 1
            assert data["results"][0]["kind"] == "dependency"


class TestSCIPDependentsTool:
    """Tests for scip_dependents MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_dependents_returns_mcp_response(
        self, mock_user, mock_scip_files
    ):
        """Should return MCP-compliant response with dependent results."""
        from code_indexer.server.mcp.handlers import scip_dependents

        params = {"symbol": "UserService", "exact": False}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.get_dependents.return_value = [
            {
                "symbol": "com.example.AuthHandler",
                "project": "/path/to/project1",
                "file_path": "src/auth/handler.py",
                "line": 20,
                "column": 5,
                "kind": "dependent",
                "relationship": "uses",
                "context": None,
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_dependents(params, mock_user)

            # Verify MCP-compliant response structure
            assert "content" in response
            assert len(response["content"]) == 1
            assert response["content"][0]["type"] == "text"

            # Parse JSON response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["symbol"] == "UserService"
            assert data["total_results"] >= 1
            assert len(data["results"]) >= 1
            assert data["results"][0]["kind"] == "dependent"


class TestSCIPImpactTool:
    """Tests for scip_impact MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_impact_returns_mcp_response(self, mock_user, tmp_path):
        """Should return MCP-compliant response with impact analysis results."""
        from code_indexer.server.mcp.handlers import scip_impact

        params = {"symbol": "UserService", "depth": 3}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.analyze_impact.return_value = {
            "target_symbol": "com.example.UserService",
            "depth_analyzed": 3,
            "total_affected": 1,
            "truncated": False,
            "affected_symbols": [
                {
                    "symbol": "com.example.AuthHandler",
                    "file_path": "src/auth/handler.py",
                    "line": 20,
                    "column": 5,
                    "depth": 1,
                    "relationship": "call",
                    "chain": ["com.example.UserService", "com.example.AuthHandler"],
                }
            ],
            "affected_files": [
                {
                    "path": "src/auth/handler.py",
                    "project": "project1",
                    "affected_symbol_count": 1,
                    "min_depth": 1,
                    "max_depth": 1,
                }
            ],
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_impact(params, mock_user)

            # Verify MCP-compliant response structure
            assert "content" in response
            assert len(response["content"]) == 1
            assert response["content"][0]["type"] == "text"

            # Parse JSON response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["target_symbol"] == "com.example.UserService"
            assert data["depth_analyzed"] == 3
            assert data["total_affected"] == 1
            assert "affected_symbols" in data
            assert len(data["affected_symbols"]) == 1


class TestSCIPCallChainTool:
    """Tests for scip_callchain MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_callchain_returns_mcp_response(self, mock_user):
        """Should return MCP-compliant response with call chain results."""
        from code_indexer.server.mcp.handlers import scip_callchain

        params = {"from_symbol": "Controller", "to_symbol": "Database"}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.trace_callchain.return_value = [
            {
                "path": ["Controller", "Service", "Database"],
                "length": 3,
                "has_cycle": False,
            }
        ]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_callchain(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["from_symbol"] == "Controller"
            assert data["to_symbol"] == "Database"
            assert data["total_chains_found"] == 1


class TestSCIPContextTool:
    """Tests for scip_context MCP tool."""

    @pytest.mark.asyncio
    async def test_scip_context_returns_mcp_response(self, mock_user):
        """Should return MCP-compliant response with smart context results."""
        from code_indexer.server.mcp.handlers import scip_context

        params = {"symbol": "UserService"}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.get_context.return_value = {
            "target_symbol": "UserService",
            "summary": "Read these 1 file(s)",
            "files": [
                {
                    "path": "src/service.py",
                    "project": "backend",
                    "relevance_score": 0.9,
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
                    "read_priority": 1,
                }
            ],
            "total_files": 1,
            "total_symbols": 1,
            "avg_relevance": 0.9,
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = await scip_context(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["target_symbol"] == "UserService"
            assert data["total_files"] == 1
