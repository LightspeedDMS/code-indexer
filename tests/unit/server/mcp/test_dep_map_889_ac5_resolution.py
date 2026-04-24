"""
Story #889 AC5 — hub tool resolution states.

Uses Story #888 ResolutionLiteral + assert_resolution_valid + assert_success_resolution_consistent.

Covers:
- Unknown by → resolution=invalid_input, success=False
- All three valid by values → resolution=ok, success=True
- invalid_input present in ResolutionLiteral
- error field present on invalid_input response
- Missing dep_map_path returns success=False with valid resolution
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers._depmap_aliases import (
    assert_resolution_valid,
    assert_success_resolution_consistent,
)

from tests.unit.server.mcp.test_dep_map_889_fixtures import (
    _call_hub,
    _make_hub_graph,
    _make_user,
)


class TestAC5InvalidByResolution:
    """Unknown by= values return resolution=invalid_input via Story #888 helpers."""

    def test_degree_of_separation_returns_invalid_input(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "degree_of_separation"}, root)
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
        assert data["success"] is False
        assert data["resolution"] == "invalid_input"

    def test_foobar_returns_invalid_input(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "foobar"}, root)
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
        assert data["resolution"] == "invalid_input"
        assert data["success"] is False

    def test_invalid_input_response_has_error_field(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "bad_value"}, root)
        assert "error" in data, "invalid_input response must include 'error' field"
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])


class TestAC5ValidByResolution:
    """Valid by= values return resolution=ok via Story #888 helpers."""

    def test_out_degree_returns_ok(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "out_degree"}, root)
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
        assert data["resolution"] == "ok"
        assert data["success"] is True

    def test_in_degree_returns_ok(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "in_degree"}, root)
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
        assert data["resolution"] == "ok"
        assert data["success"] is True

    def test_total_degree_returns_ok(self, tmp_path: Path) -> None:
        root = _make_hub_graph(tmp_path)
        data = _call_hub({"by": "total_degree"}, root)
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
        assert data["resolution"] == "ok"
        assert data["success"] is True


class TestAC5ResolutionLiteralContract:
    """invalid_input is in ResolutionLiteral; missing path returns valid resolution."""

    def test_invalid_input_in_resolution_literal(self) -> None:
        import typing
        from code_indexer.server.mcp.handlers._depmap_aliases import ResolutionLiteral

        args = set(typing.get_args(ResolutionLiteral))
        assert "invalid_input" in args

    def test_missing_dep_map_path_returns_success_false(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_hub_domains_handler,
        )

        state = MagicMock()
        state.dependency_map_service.cidx_meta_read_path = tmp_path / "no-such-dir"
        with patch(
            "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
            state,
        ):
            result = depmap_get_hub_domains_handler({}, _make_user())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert_resolution_valid(data["resolution"])
        assert_success_resolution_consistent(data["success"], data["resolution"])
