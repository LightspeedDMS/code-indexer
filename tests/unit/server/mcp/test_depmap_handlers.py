"""
Unit tests for depmap MCP handlers — Story #855.

Tests: fresh path read per call, success with anomalies, missing path failure,
handler registered in HANDLER_REGISTRY.
"""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


def _make_app_state(read_path: Path) -> MagicMock:
    """Build a mock app state whose dependency_map_service.cidx_meta_read_path returns read_path."""
    state = MagicMock()
    state.dependency_map_service.cidx_meta_read_path = read_path
    return state


def _call_handler(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import depmap_find_consumers_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_find_consumers_handler(params, _make_user())


def _parse_response(result: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_handler_reads_path_fresh_each_call(tmp_path: Path) -> None:
    """cidx_meta_read_path property is accessed once per handler invocation."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text("[]", encoding="utf-8")

    call_count = 0

    class _TrackingService:
        @property
        def cidx_meta_read_path(self) -> Path:
            nonlocal call_count
            call_count += 1
            return tmp_path

    state = MagicMock()
    state.dependency_map_service = _TrackingService()

    _call_handler({"repo_name": "any-repo"}, state)
    _call_handler({"repo_name": "any-repo"}, state)

    assert call_count == 2, (
        f"Expected cidx_meta_read_path accessed once per call (total 2), got {call_count}"
    )


def test_handler_returns_success_true_with_anomalies(tmp_path: Path) -> None:
    """Handler returns success=true and surfaces anomalies when a domain file is malformed."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text(
        '[{"name":"dom","description":"d","participating_repos":["repo-a","repo-b"]}]',
        encoding="utf-8",
    )
    (dep_map_dir / "dom.md").write_text(
        "---\nname: [unclosed\nbroken: :\n---\n# bad\n", encoding="utf-8"
    )

    result = _call_handler({"repo_name": "repo-b"}, _make_app_state(tmp_path))
    data = _parse_response(result)

    assert data["success"] is True
    assert isinstance(data["consumers"], list)
    assert isinstance(data["anomalies"], list)
    assert len(data["anomalies"]) >= 1


def test_handler_returns_success_false_when_path_missing(tmp_path: Path) -> None:
    """When dep_map_path does not exist, handler returns success=false with error."""
    state = _make_app_state(tmp_path / "no-such-dir")

    result = _call_handler({"repo_name": "any-repo"}, state)
    data = _parse_response(result)

    assert data["success"] is False
    assert "error" in data
    assert data["consumers"] == []
    assert data["anomalies"] == []


def test_handler_registered_in_handler_registry() -> None:
    """depmap_find_consumers is present in HANDLER_REGISTRY after module import."""
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    assert "depmap_find_consumers" in HANDLER_REGISTRY
