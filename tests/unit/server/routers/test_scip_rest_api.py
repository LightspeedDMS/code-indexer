"""Unit tests for SCIP REST API router.

Story #41: Updated tests to use SCIPQueryService delegation pattern.
Story #50: Updated tests to use sync mocks since route handlers are now sync.
"""

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_user


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
    service = Mock()
    return service


@pytest.fixture
def test_client(mock_user):
    """Create a test client with auth mocked."""
    from code_indexer.server.app import app

    # Override auth dependency to return mock user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    client = TestClient(app)
    yield client

    # Clean up overrides
    app.dependency_overrides.clear()


class TestDefinitionEndpoint:
    """Tests for /scip/definition endpoint."""

    def test_definition_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return aggregated definition results."""
        # Mock service to return definition results
        mock_scip_service.find_definition.return_value = [
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
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Story #50: Use regular patch since function is now sync
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.find_definition.return_value
                )
                response = test_client.get("/scip/definition?symbol=UserService")

                assert response.status_code == 200
                data = response.json()

                assert data["success"] is True
                assert data["symbol"] == "UserService"
                assert data["total_results"] >= 1
                assert "results" in data
                assert len(data["results"]) >= 1

                # Verify result structure
                result = data["results"][0]
                assert "symbol" in result
                assert "project" in result
                assert "file_path" in result
                assert "line" in result
                assert "column" in result
                assert result["kind"] == "definition"


class TestReferencesEndpoint:
    """Tests for /scip/references endpoint."""

    def test_references_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return aggregated reference results."""
        mock_scip_service.find_references.return_value = [
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
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Story #50: Use regular patch since function is now sync
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.find_references.return_value
                )
                response = test_client.get(
                    "/scip/references?symbol=UserService&limit=100"
                )

                assert response.status_code == 200
                data = response.json()

                assert data["success"] is True
                assert data["symbol"] == "UserService"
                assert data["total_results"] >= 1
                assert len(data["results"]) >= 1
                assert data["results"][0]["kind"] == "reference"

    def test_references_endpoint_rejects_out_of_range_limit_with_422(self, test_client):
        """limit=0 must be rejected by FastAPI's Query validation with HTTP
        422. Downstream, find_references (scip/database/queries.py) treats
        limit=0 as "unlimited" (conditionally omits the SQL LIMIT clause),
        so an unbounded limit parameter at this REST front door is a live
        resource-exhaustion gap -- found during the #1599/#1602/#1604
        remediation review, fixed now per this project's no-deferral
        practice."""
        response = test_client.get("/scip/references?symbol=UserService&limit=0")

        assert response.status_code == 422


class TestDependenciesEndpoint:
    """Tests for /scip/dependencies endpoint."""

    def test_dependencies_endpoint_returns_results(
        self, test_client, mock_scip_service
    ):
        """Should call SCIPQueryService and return aggregated dependency results."""
        mock_scip_service.get_dependencies.return_value = [
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
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Story #50: Use regular patch since function is now sync
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.get_dependencies.return_value
                )
                response = test_client.get(
                    "/scip/dependencies?symbol=UserService&depth=1"
                )

                assert response.status_code == 200
                data = response.json()

                assert data["success"] is True
                assert data["symbol"] == "UserService"
                assert data["total_results"] >= 1
                assert len(data["results"]) >= 1
                assert data["results"][0]["kind"] == "dependency"

    def test_dependencies_endpoint_rejects_out_of_range_depth_with_422(
        self, test_client
    ):
        """depth=0 (below the [1, 10] bound) must be rejected by FastAPI's
        Query validation with HTTP 422, matching the /scip/impact route's
        existing ge=1,le=10 convention. Without this bound, an out-of-range
        depth silently reaches the same swallowed-ValueError defect fixed
        at the MCP handler layer for Bug #1599/#1602/#1604."""
        response = test_client.get("/scip/dependencies?symbol=UserService&depth=0")

        assert response.status_code == 422


class TestDependentsEndpoint:
    """Tests for /scip/dependents endpoint."""

    def test_dependents_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return aggregated dependent results."""
        mock_scip_service.get_dependents.return_value = [
            {
                "symbol": "com.example.AuthHandler",
                "project": "/path/to/project1",
                "file_path": "src/auth/handler.py",
                "line": 20,
                "column": 5,
                "kind": "dependent",
                "relationship": "call",
                "context": None,
            }
        ]

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            # Story #50: Use regular patch since function is now sync
            with patch(
                "code_indexer.server.routers.scip_queries._apply_scip_payload_truncation",
            ) as mock_truncate:
                mock_truncate.return_value = (
                    mock_scip_service.get_dependents.return_value
                )
                response = test_client.get(
                    "/scip/dependents?symbol=UserService&depth=1"
                )

                assert response.status_code == 200
                data = response.json()

                assert data["success"] is True
                assert data["symbol"] == "UserService"
                assert data["total_results"] >= 1
                assert len(data["results"]) >= 1
                assert data["results"][0]["kind"] == "dependent"

    def test_dependents_endpoint_rejects_out_of_range_depth_with_422(self, test_client):
        """depth=0 (below the [1, 10] bound) must be rejected by FastAPI's
        Query validation with HTTP 422, matching the /scip/impact route's
        existing ge=1,le=10 convention. Without this bound, an out-of-range
        depth silently reaches the same swallowed-ValueError defect fixed
        at the MCP handler layer for Bug #1599/#1602/#1604."""
        response = test_client.get("/scip/dependents?symbol=UserService&depth=0")

        assert response.status_code == 422


class TestImpactEndpoint:
    """Tests for /scip/impact endpoint."""

    def test_impact_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return impact analysis results."""
        mock_scip_service.analyze_impact.return_value = {
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
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = test_client.get("/scip/impact?symbol=UserService&depth=3")

            assert response.status_code == 200
            data = response.json()

            assert data["success"] is True
            assert data["target_symbol"] == "com.example.UserService"
            assert data["depth_analyzed"] == 3
            assert data["total_affected"] == 1
            assert "affected_symbols" in data
            assert "affected_files" in data
            assert len(data["affected_symbols"]) == 1
            assert data["affected_symbols"][0]["symbol"] == "com.example.AuthHandler"


class TestCallChainEndpoint:
    """Tests for /scip/callchain endpoint."""

    def test_callchain_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return call chain results."""
        mock_scip_service.trace_callchain.return_value = (
            [
                {
                    "path": [
                        "com.example.Controller",
                        "com.example.Service",
                        "com.example.Database",
                    ],
                    "length": 3,
                    "has_cycle": False,
                }
            ],
            [],
        )

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = test_client.get(
                "/scip/callchain?from_symbol=Controller&to_symbol=Database&max_depth=3"
            )

            assert response.status_code == 200
            data = response.json()

            assert data["success"] is True
            assert data["from_symbol"] == "Controller"
            assert data["to_symbol"] == "Database"
            assert data["total_chains_found"] == 1
            assert "chains" in data
            assert len(data["chains"]) == 1

    def test_callchain_endpoint_accepts_max_depth_at_new_cap_of_3(
        self, test_client, mock_scip_service
    ):
        """max_depth=3 (the new upper bound) must still be accepted (200),
        pinning the top edge of the narrowed [1, 3] callchain contract
        (Bug #1603)."""
        mock_scip_service.trace_callchain.return_value = ([], [])

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = test_client.get(
                "/scip/callchain?from_symbol=Controller&to_symbol=Database&max_depth=3"
            )

        assert response.status_code == 200

    def test_callchain_endpoint_surfaces_timeout_as_failure(
        self, test_client, mock_scip_service
    ):
        """Bug #1603 code review (Priority 1): a non-empty timeout_errors
        from SCIPQueryService.trace_callchain must produce success: False
        with an explicit error, NOT an indistinguishable empty-success
        response (total_chains_found: 0 looks identical to "no chains
        exist" while actually meaning "the query was cut off")."""
        mock_scip_service.trace_callchain.return_value = (
            [],
            ["Query exceeded 30-second timeout."],
        )

        with patch(
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = test_client.get(
                "/scip/callchain?from_symbol=Controller&to_symbol=Database&max_depth=3"
            )

            assert response.status_code == 200
            data = response.json()

            assert data["success"] is False, (
                f"Expected success: False on timeout, got: {data}"
            )
            assert "error" in data and data["error"], (
                f"Expected a non-empty error message, got: {data}"
            )
            assert "timeout" in data["error"].lower()

    def test_callchain_endpoint_rejects_out_of_range_max_depth_with_422(
        self, test_client
    ):
        """max_depth=4 (the first value above the NEW [1, 3] callchain-only
        bound) must be rejected by FastAPI's Query validation with HTTP 422.

        Bug #1603 narrows /scip/callchain's max_depth bound from the old
        [1, 10] (shared with /scip/impact and the MCP scip_callchain
        handler's then-shared _MIN_SCIP_DEPTH/_MAX_SCIP_DEPTH) down to
        [1, 3], to match trace_call_chain_v2/_batched's REAL MAX_DEPTH_CAP
        in queries.py -- depths 4-10 were previously silently downgraded to
        3 by that layer anyway (a WARNING, not an error), so the old [1, 10]
        REST contract advertised depths the backend never actually honored.
        This bound is deliberately DIFFERENT from /scip/impact, dependents,
        and dependencies, which remain [1, 10] -- unaffected by this change.
        """
        response = test_client.get(
            "/scip/callchain?from_symbol=Controller&to_symbol=Database&max_depth=4"
        )

        assert response.status_code == 422

    def test_callchain_endpoint_rejects_max_depth_below_min_with_422(self, test_client):
        """max_depth=0 (below the [1, 3] callchain-only floor) must be
        rejected by FastAPI's Query validation with HTTP 422, pinning the
        bottom edge of the narrowed contract (Bug #1603)."""
        response = test_client.get(
            "/scip/callchain?from_symbol=Controller&to_symbol=Database&max_depth=0"
        )

        assert response.status_code == 422


class TestContextEndpoint:
    """Tests for /scip/context endpoint."""

    def test_context_endpoint_returns_results(self, test_client, mock_scip_service):
        """Should call SCIPQueryService and return smart context results."""
        mock_scip_service.get_context.return_value = {
            "target_symbol": "com.example.UserService",
            "summary": "Read these 1 file(s) to understand com.example.UserService",
            "files": [
                {
                    "path": "src/services/user_service.py",
                    "project": "backend",
                    "relevance_score": 0.9,
                    "symbols": [
                        {
                            "name": "com.example.UserService",
                            "kind": "class",
                            "relationship": "definition",
                            "line": 10,
                            "column": 5,
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
            "code_indexer.server.routers.scip_queries._get_scip_query_service",
            return_value=mock_scip_service,
        ):
            response = test_client.get("/scip/context?symbol=UserService&limit=20")

            assert response.status_code == 200
            data = response.json()

            assert data["success"] is True
            assert data["target_symbol"] == "com.example.UserService"
            assert data["total_files"] == 1
            assert data["total_symbols"] == 1
            assert "files" in data
            assert len(data["files"]) == 1
            assert data["files"][0]["relevance_score"] == 0.9
            assert data["files"][0]["read_priority"] == 1
