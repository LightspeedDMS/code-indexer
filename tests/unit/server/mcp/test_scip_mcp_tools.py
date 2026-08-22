"""Unit tests for SCIP MCP tool handlers."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch
import json
from datetime import datetime, timezone

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

    def test_scip_definition_returns_mcp_response(self, mock_user, mock_scip_files):
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
            response = scip_definition(params, mock_user)

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

    def test_scip_references_returns_mcp_response(self, mock_user, mock_scip_files):
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
            response = scip_references(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["results"][0]["kind"] == "reference"


class TestSCIPDependenciesTool:
    """Tests for scip_dependencies MCP tool."""

    def test_scip_dependencies_returns_mcp_response(self, mock_user, mock_scip_files):
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
            response = scip_dependencies(params, mock_user)

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

    def test_scip_dependents_returns_mcp_response(self, mock_user, mock_scip_files):
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
            response = scip_dependents(params, mock_user)

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

    def test_scip_impact_returns_mcp_response(self, mock_user, tmp_path):
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
            response = scip_impact(params, mock_user)

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

    def test_scip_callchain_returns_mcp_response(self, mock_user):
        """Should return MCP-compliant response with call chain results."""
        from code_indexer.server.mcp.handlers import scip_callchain

        params = {"from_symbol": "Controller", "to_symbol": "Database"}

        # Mock SCIPQueryService (Story #40 refactoring)
        mock_service = Mock()
        mock_service.trace_callchain.return_value = (
            [
                {
                    "path": ["Controller", "Service", "Database"],
                    "length": 3,
                    "has_cycle": False,
                }
            ],
            [],
        )
        # Bug #1613: scip_callchain now derives scip_files_searched from a
        # real find_scip_files() call -- stub it (a Mock(), unlike
        # MagicMock(), has no usable __len__ by default).
        mock_service.find_scip_files.return_value = [Path("repo/index.scip.db")]

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_callchain(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["from_symbol"] == "Controller"
            assert data["to_symbol"] == "Database"
            assert data["total_chains_found"] == 1

    def test_scip_callchain_surfaces_timeout_as_failure(self, mock_user):
        """Bug #1603 code review (Priority 1): a non-empty timeout_errors
        from SCIPQueryService.trace_callchain must produce success: False
        with an explicit error, NOT an indistinguishable empty-success
        response (total_chains_found: 0 looks identical to "no chains
        exist" while actually meaning "the query was cut off")."""
        from code_indexer.server.mcp.handlers import scip_callchain

        params = {"from_symbol": "Controller", "to_symbol": "Database"}

        mock_service = Mock()
        mock_service.trace_callchain.return_value = (
            [],
            ["Query exceeded 30-second timeout."],
        )

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_callchain(params, mock_user)

            data = json.loads(response["content"][0]["text"])
            assert data["success"] is False, (
                f"Expected success: False on timeout, got: {data}"
            )
            assert "error" in data and data["error"], (
                f"Expected a non-empty error message, got: {data}"
            )
            assert "timeout" in data["error"].lower()


class TestSCIPContextTool:
    """Tests for scip_context MCP tool."""

    def test_scip_context_returns_mcp_response(self, mock_user):
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
            response = scip_context(params, mock_user)

            assert "content" in response
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is True
            assert data["target_symbol"] == "UserService"
            assert data["total_files"] == 1


class TestSCIPImpactDepthClamp:
    """Tests for scip_impact depth clamping (Bug #1599).

    scip_impact's depth parameter must be clamped to [1, 10] at the handler
    layer, mirroring the existing clamp pattern scip_callchain already uses
    for its max_depth parameter.
    """

    def test_scip_impact_depth_far_above_max_is_clamped_to_10(self, mock_user):
        """depth=100000 must be clamped down to the documented max of 10."""
        from code_indexer.server.mcp.handlers import scip_impact

        params = {"symbol": "UserService", "depth": 100000}

        mock_service = Mock()
        mock_service.analyze_impact.return_value = {
            "target_symbol": "com.example.UserService",
            "depth_analyzed": 10,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            scip_impact(params, mock_user)

        _, kwargs = mock_service.analyze_impact.call_args
        assert kwargs["depth"] == 10

    @pytest.mark.parametrize("raw_depth", [0, -5])
    def test_scip_impact_depth_below_min_is_clamped_to_1(self, mock_user, raw_depth):
        """depth=0 or a negative depth must be clamped up to the min of 1."""
        from code_indexer.server.mcp.handlers import scip_impact

        params = {"symbol": "UserService", "depth": raw_depth}

        mock_service = Mock()
        mock_service.analyze_impact.return_value = {
            "target_symbol": "com.example.UserService",
            "depth_analyzed": 1,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            scip_impact(params, mock_user)

        _, kwargs = mock_service.analyze_impact.call_args
        assert kwargs["depth"] == 1

    def test_scip_impact_default_depth_unchanged(self, mock_user):
        """No depth param supplied must still default to 3, unaffected by
        the new clamp (3 is already within [1, 10])."""
        from code_indexer.server.mcp.handlers import scip_impact

        params = {"symbol": "UserService"}

        mock_service = Mock()
        mock_service.analyze_impact.return_value = {
            "target_symbol": "com.example.UserService",
            "depth_analyzed": 3,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            scip_impact(params, mock_user)

        _, kwargs = mock_service.analyze_impact.call_args
        assert kwargs["depth"] == 3

    @pytest.mark.parametrize("bad_max_depth", [100000, -3])
    def test_scip_callchain_max_depth_out_of_range_is_rejected(
        self, mock_user, bad_max_depth
    ):
        """Bug #1603 code review (Priority 2, item 4): scip_callchain must
        REJECT an out-of-range max_depth (success: False, explicit error),
        matching the REST route's FastAPI Query(le=3) HTTP 422 behavior --
        NOT silently clamp it to 3 with only a server-side WARNING log (the
        pre-remediation behavior this test replaces). trace_callchain must
        never even be called for a rejected value."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_service = Mock()
        mock_service.trace_callchain.return_value = ([], [])

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_callchain(
                {
                    "from_symbol": "Controller",
                    "to_symbol": "Database",
                    "max_depth": bad_max_depth,
                },
                mock_user,
            )
            data = json.loads(response["content"][0]["text"])
            assert data["success"] is False
            assert "max_depth" in data["error"]
            mock_service.trace_callchain.assert_not_called()

    def test_scip_callchain_max_depth_accepts_string_value(self, mock_user):
        """Bug #1603 code review round 2 (Priority 3, item D): max_depth
        used to be read via a direct params.get("max_depth", 3) instead
        of the _coerce_int helper every sibling handler in this file uses
        (scip_dependencies, scip_dependents, scip_impact, scip_context)
        -- a JSON client sending max_depth as a string ("3") would hit a
        TypeError in the subsequent int comparison against
        _MAX_CALLCHAIN_DEPTH instead of being coerced cleanly."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_service = Mock()
        mock_service.trace_callchain.return_value = ([], [])
        # Bug #1613: scip_callchain now derives scip_files_searched from a
        # real find_scip_files() call -- stub it (a Mock(), unlike
        # MagicMock(), has no usable __len__ by default).
        mock_service.find_scip_files.return_value = []

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_callchain(
                {
                    "from_symbol": "Controller",
                    "to_symbol": "Database",
                    "max_depth": "3",
                },
                mock_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True, (
            f"Expected string max_depth to be coerced cleanly, got: {data}"
        )
        _, kwargs = mock_service.trace_callchain.call_args
        assert kwargs["max_depth"] == 3


def _call_scip_dependents_with_mock_service(params, mock_user, mock_service):
    """Shared helper: patch _get_scip_query_service and invoke scip_dependents.

    Used by TestSCIPDependentsDepthClamp to avoid repeating the same
    patch-context boilerplate in every test method.
    """
    from code_indexer.server.mcp.handlers import scip_dependents

    with patch(
        "code_indexer.server.mcp.handlers._get_scip_query_service",
        return_value=mock_service,
    ):
        return scip_dependents(params, mock_user)


class TestSCIPDependentsDepthClamp:
    """Tests for scip_dependents depth validation (Bug #1602 depth-clamp
    family; contract corrected by Bug #1614).

    Bug #1614: scip_dependents's depth parameter must be REJECTED (success:
    False, empty results, service never called) when out of the documented
    [1, 10] range -- mirroring scip_callchain's existing loud-reject
    contract for max_depth (Bug #1603) and the REST/service-layer siblings
    (GET /scip/dependents depth=0/11 -> HTTP 422; POST
    /api/scip/multi/dependents max_depth=0 -> structured error). Silently
    clamping to the nearest bound (the pre-#1614 behavior this test class
    replaces) hid the caller's mistake behind a misleading success:true.
    """

    @pytest.mark.parametrize("raw_depth", [100000, 11])
    def test_scip_dependents_depth_above_max_is_rejected(self, mock_user, raw_depth):
        """depth above the documented max of 10 must be rejected loudly."""
        mock_service = Mock()
        mock_service.get_dependents.return_value = []

        response = _call_scip_dependents_with_mock_service(
            {"symbol": "UserService", "depth": raw_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "depth" in data["error"]
        assert str(raw_depth) in data["error"]
        assert data["results"] == []
        mock_service.get_dependents.assert_not_called()

    @pytest.mark.parametrize("raw_depth", [0, -5])
    def test_scip_dependents_depth_below_min_is_rejected(self, mock_user, raw_depth):
        """depth=0 or a negative depth must be rejected loudly rather than
        clamped up to the min of 1."""
        mock_service = Mock()
        mock_service.get_dependents.return_value = []

        response = _call_scip_dependents_with_mock_service(
            {"symbol": "UserService", "depth": raw_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "depth" in data["error"]
        assert str(raw_depth) in data["error"]
        assert data["results"] == []
        mock_service.get_dependents.assert_not_called()

    def test_scip_dependents_default_depth_unchanged(self, mock_user):
        """No depth param supplied must still default to 1 (scip_dependents'
        documented default, NOT 3 like scip_impact), unaffected by the new
        rejection guard (1 is already within [1, 10])."""
        mock_service = Mock()
        mock_service.get_dependents.return_value = []

        _call_scip_dependents_with_mock_service(
            {"symbol": "UserService"}, mock_user, mock_service
        )

        _, kwargs = mock_service.get_dependents.call_args
        assert kwargs["depth"] == 1

    @pytest.mark.parametrize("valid_depth", [1, 5, 10])
    def test_scip_dependents_depth_within_range_still_succeeds(
        self, mock_user, valid_depth
    ):
        """Every depth within the documented [1, 10] range must still
        succeed unaffected by the new rejection guard."""
        mock_service = Mock()
        mock_service.get_dependents.return_value = [
            {"symbol": "com.example.AuthHandler", "kind": "dependent"}
        ]

        response = _call_scip_dependents_with_mock_service(
            {"symbol": "UserService", "depth": valid_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        _, kwargs = mock_service.get_dependents.call_args
        assert kwargs["depth"] == valid_depth


def _call_scip_dependencies_with_mock_service(params, mock_user, mock_service):
    """Shared helper: patch _get_scip_query_service and invoke scip_dependencies.

    Used by TestSCIPDependenciesDepthClamp to avoid repeating the same
    patch-context boilerplate in every test method.
    """
    from code_indexer.server.mcp.handlers import scip_dependencies

    with patch(
        "code_indexer.server.mcp.handlers._get_scip_query_service",
        return_value=mock_service,
    ):
        return scip_dependencies(params, mock_user)


class TestSCIPDependenciesDepthClamp:
    """Tests for scip_dependencies depth validation (Bug #1604 depth-clamp
    family; contract corrected by Bug #1614).

    Bug #1614: scip_dependencies's depth parameter must be REJECTED
    (success: False, empty results, service never called) when out of the
    documented [1, 10] range -- mirroring scip_callchain's existing
    loud-reject contract for max_depth (Bug #1603) and the REST/
    service-layer siblings. Silently clamping to the nearest bound (the
    pre-#1614 behavior this test class replaces) hid the caller's mistake
    behind a misleading success:true.
    """

    @pytest.mark.parametrize("raw_depth", [100000, 11])
    def test_scip_dependencies_depth_above_max_is_rejected(self, mock_user, raw_depth):
        """depth above the documented max of 10 must be rejected loudly."""
        mock_service = Mock()
        mock_service.get_dependencies.return_value = []

        response = _call_scip_dependencies_with_mock_service(
            {"symbol": "UserService", "depth": raw_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "depth" in data["error"]
        assert str(raw_depth) in data["error"]
        assert data["results"] == []
        mock_service.get_dependencies.assert_not_called()

    @pytest.mark.parametrize("raw_depth", [0, -5])
    def test_scip_dependencies_depth_below_min_is_rejected(self, mock_user, raw_depth):
        """depth=0 or a negative depth must be rejected loudly rather than
        clamped up to the min of 1."""
        mock_service = Mock()
        mock_service.get_dependencies.return_value = []

        response = _call_scip_dependencies_with_mock_service(
            {"symbol": "UserService", "depth": raw_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "depth" in data["error"]
        assert str(raw_depth) in data["error"]
        assert data["results"] == []
        mock_service.get_dependencies.assert_not_called()

    def test_scip_dependencies_default_depth_unchanged(self, mock_user):
        """No depth param supplied must still default to 1 (scip_dependencies'
        documented default), unaffected by the new rejection guard (1 is
        already within [1, 10])."""
        mock_service = Mock()
        mock_service.get_dependencies.return_value = []

        _call_scip_dependencies_with_mock_service(
            {"symbol": "UserService"}, mock_user, mock_service
        )

        _, kwargs = mock_service.get_dependencies.call_args
        assert kwargs["depth"] == 1

    @pytest.mark.parametrize("valid_depth", [1, 5, 10])
    def test_scip_dependencies_depth_within_range_still_succeeds(
        self, mock_user, valid_depth
    ):
        """Every depth within the documented [1, 10] range must still
        succeed unaffected by the new rejection guard."""
        mock_service = Mock()
        mock_service.get_dependencies.return_value = [
            {"symbol": "com.example.Database", "kind": "dependency"}
        ]

        response = _call_scip_dependencies_with_mock_service(
            {"symbol": "UserService", "depth": valid_depth}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        _, kwargs = mock_service.get_dependencies.call_args
        assert kwargs["depth"] == valid_depth


def _call_scip_references_with_mock_service(params, mock_user, mock_service):
    """Shared helper: patch _get_scip_query_service and invoke scip_references.

    Mirrors _call_scip_dependents_with_mock_service for the limit-clamp tests.
    """
    from code_indexer.server.mcp.handlers import scip_references

    with patch(
        "code_indexer.server.mcp.handlers._get_scip_query_service",
        return_value=mock_service,
    ):
        return scip_references(params, mock_user)


class TestSCIPReferencesLimitClamp:
    """Tests for scip_references limit clamping (last mechanical fix in the
    scip depth-clamp remediation family, Bugs #1599/#1602/#1604).

    scip_references's limit parameter must be clamped to [1, 10000] at the
    handler layer, mirroring the REST sibling GET /scip/references route
    (Query(..., ge=1, le=10000)) and the depth-clamp idiom already used for
    scip_impact/scip_callchain/scip_dependents/scip_dependencies.
    """

    def test_scip_references_limit_far_above_max_is_clamped_to_10000(self, mock_user):
        """limit=1000000 must be clamped down to the documented max of 10000."""
        mock_service = Mock()
        mock_service.find_references.return_value = []

        _call_scip_references_with_mock_service(
            {"symbol": "UserService", "limit": 1000000}, mock_user, mock_service
        )

        _, kwargs = mock_service.find_references.call_args
        assert kwargs["limit"] == 10000

    def test_scip_references_limit_zero_is_clamped_to_1_not_unlimited(self, mock_user):
        """limit=0 must be clamped up to the min of 1, NOT treated as
        unlimited (the real find_references semantics for limit<=0)."""
        mock_service = Mock()
        mock_service.find_references.return_value = []

        _call_scip_references_with_mock_service(
            {"symbol": "UserService", "limit": 0}, mock_user, mock_service
        )

        _, kwargs = mock_service.find_references.call_args
        assert kwargs["limit"] == 1

    def test_scip_references_default_limit_unchanged(self, mock_user):
        """No limit param supplied must still default to 100, unaffected by
        the new clamp (100 is already within [1, 10000])."""
        mock_service = Mock()
        mock_service.find_references.return_value = []

        _call_scip_references_with_mock_service(
            {"symbol": "UserService"}, mock_user, mock_service
        )

        _, kwargs = mock_service.find_references.call_args
        assert kwargs["limit"] == 100


def _make_limit_validating_find_references(all_results):
    """Build a find_references side_effect mimicking the real
    queries.py/find_references behavior: limit<=0 means UNLIMITED (returns
    every row), otherwise returns at most `limit` rows.

    Shared by TestSCIPReferencesLimitClampRegression's real-behavior test to
    prove the fix removes the resource-exhaustion gap rather than merely
    running a clamp function.
    """

    def _side_effect(**kwargs):
        limit = kwargs["limit"]
        if limit <= 0:
            return list(all_results)
        return list(all_results)[:limit]

    return _side_effect


class TestSCIPReferencesLimitClampRegression:
    """Regression test for scip_references limit clamping (Bugs
    #1599/#1602/#1604 family, final mechanical fix).

    Before the fix, an unclamped limit<=0 reached the real
    find_references UNLIMITED path (queries.py: 'Conditionally add LIMIT
    clause (limit=0 means unlimited)') -- a live resource-exhaustion gap at
    the MCP front door. After the fix, the clamp means the service never
    sees the raw out-of-range value.
    """

    def test_scip_references_limit_zero_no_longer_returns_unbounded_results(
        self, mock_user
    ):
        """Real-behavior test: before the fix, limit=0 reached the real
        find_references unlimited path and returned every matching row.
        After the fix, the clamp means the service always receives limit=1,
        so it returns at most 1 result -- proving the resource-exhaustion
        gap is closed, not just that a clamp function ran."""
        real_references = [
            {"symbol": "com.example.AuthHandler", "kind": "reference"},
            {"symbol": "com.example.SessionManager", "kind": "reference"},
            {"symbol": "com.example.TokenValidator", "kind": "reference"},
        ]

        mock_service = Mock()
        mock_service.find_references.side_effect = (
            _make_limit_validating_find_references(real_references)
        )

        response = _call_scip_references_with_mock_service(
            {"symbol": "UserService", "limit": 0}, mock_user, mock_service
        )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is True
        assert data["total_results"] == 1

        _, kwargs = mock_service.find_references.call_args
        assert kwargs["limit"] == 1
