"""
Unit tests for depmap MCP handlers — Stories #855 and #856.

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


# ---------------------------------------------------------------------------
# S2 test helpers — file setup and state factories
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 — import at module scope for helpers below


def _write_domains_json_s2(dep_map_dir: Path, domains: list) -> None:
    """Write _domains.json into dep_map_dir for S2 handler tests."""
    (dep_map_dir / "_domains.json").write_text(_json.dumps(domains), encoding="utf-8")


def _write_domain_md_s2(
    dep_map_dir: Path, domain_name: str, repo: str, role: str
) -> None:
    """Write a minimal domain .md with a Repository Roles table row."""
    content = (
        f"---\ndomain: {domain_name}\nparticipating_repos:\n  - {repo}\n---\n"
        f"# Domain Analysis: {domain_name}\n\n"
        f"## Repository Roles\n\n| Repository | Language | Role |\n|---|---|---|\n"
        f"| {repo} | Python | {role} |\n\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    (dep_map_dir / f"{domain_name}.md").write_text(content, encoding="utf-8")


def _make_tracking_state(base_path: Path):
    """Return (state, counter) where state.dependency_map_service.cidx_meta_read_path
    returns base_path and increments counter on every access."""
    counter = {"count": 0}

    class _TrackingService:
        @property
        def cidx_meta_read_path(self) -> Path:
            counter["count"] += 1
            return base_path

    state = MagicMock()
    state.dependency_map_service = _TrackingService()
    return state, counter


def _call_repo_domains_handler(params: dict, app_state: MagicMock) -> Any:
    """Call depmap_get_repo_domains_handler with a patched app state."""
    from code_indexer.server.mcp.handlers.depmap import depmap_get_repo_domains_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_repo_domains_handler(params, _make_user())


def _call_domain_summary_handler(params: dict, app_state: MagicMock) -> Any:
    """Call depmap_get_domain_summary_handler with a patched app state."""
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_domain_summary_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_domain_summary_handler(params, _make_user())


# ---------------------------------------------------------------------------
# S2: depmap_get_repo_domains handler tests
# ---------------------------------------------------------------------------


def test_get_repo_domains_handler_returns_success_shape(tmp_path: Path) -> None:
    """Handler returns success=true with domains list (domain_name+role) and anomalies."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_domains_json_s2(
        dep_map_dir,
        [{"name": "dom-a", "description": "d", "participating_repos": ["my-repo"]}],
    )
    _write_domain_md_s2(dep_map_dir, "dom-a", "my-repo", "Core service")

    result = _call_repo_domains_handler(
        {"repo_name": "my-repo"}, _make_app_state(tmp_path)
    )
    data = _parse_response(result)

    assert data["success"] is True
    assert isinstance(data["domains"], list)
    assert isinstance(data["anomalies"], list)
    assert len(data["domains"]) == 1
    assert data["domains"][0]["domain_name"] == "dom-a"
    assert data["domains"][0]["role"] == "Core service"


def test_get_repo_domains_handler_missing_path_returns_success_false(
    tmp_path: Path,
) -> None:
    """When dep_map_path does not exist, handler returns success=false with error."""
    result = _call_repo_domains_handler(
        {"repo_name": "any-repo"}, _make_app_state(tmp_path / "no-such-dir")
    )
    data = _parse_response(result)

    assert data["success"] is False
    assert "error" in data
    assert data["domains"] == []
    assert data["anomalies"] == []


def test_get_repo_domains_handler_reads_path_fresh_each_call(tmp_path: Path) -> None:
    """cidx_meta_read_path is accessed once per handler invocation."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_domains_json_s2(dep_map_dir, [])

    state, counter = _make_tracking_state(tmp_path)
    _call_repo_domains_handler({"repo_name": "x"}, state)
    _call_repo_domains_handler({"repo_name": "x"}, state)

    assert counter["count"] == 2, (
        f"Expected cidx_meta_read_path accessed once per call (total 2), got {counter['count']}"
    )


def test_get_repo_domains_handler_registered_in_registry() -> None:
    """depmap_get_repo_domains is present in HANDLER_REGISTRY after module import."""
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    assert "depmap_get_repo_domains" in HANDLER_REGISTRY


# ---------------------------------------------------------------------------
# S2: depmap_get_domain_summary handler tests
# ---------------------------------------------------------------------------


def test_get_domain_summary_handler_returns_success_shape(tmp_path: Path) -> None:
    """Handler returns success=true with summary dict (name, description,
    participating_repos, cross_domain_connections) and anomalies list."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_domains_json_s2(
        dep_map_dir,
        [
            {
                "name": "my-domain",
                "description": "A test domain",
                "participating_repos": ["repo-x"],
            }
        ],
    )
    _write_domain_md_s2(dep_map_dir, "my-domain", "repo-x", "Core service")

    result = _call_domain_summary_handler(
        {"domain_name": "my-domain"}, _make_app_state(tmp_path)
    )
    data = _parse_response(result)

    assert data["success"] is True
    assert isinstance(data["anomalies"], list)
    summary = data["summary"]
    assert summary is not None
    assert summary["name"] == "my-domain"
    assert summary["description"] == "A test domain"
    assert isinstance(summary["participating_repos"], list)
    assert isinstance(summary["cross_domain_connections"], list)
    assert any(r["repo"] == "repo-x" for r in summary["participating_repos"])


def test_get_domain_summary_handler_missing_path_returns_success_false(
    tmp_path: Path,
) -> None:
    """When dep_map_path does not exist, handler returns success=false with error."""
    result = _call_domain_summary_handler(
        {"domain_name": "any-domain"}, _make_app_state(tmp_path / "no-such-dir")
    )
    data = _parse_response(result)

    assert data["success"] is False
    assert "error" in data
    assert data["summary"] is None
    assert data["anomalies"] == []


def test_get_domain_summary_handler_reads_path_fresh_each_call(tmp_path: Path) -> None:
    """cidx_meta_read_path is accessed once per handler invocation."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    _write_domains_json_s2(dep_map_dir, [])

    state, counter = _make_tracking_state(tmp_path)
    _call_domain_summary_handler({"domain_name": "x"}, state)
    _call_domain_summary_handler({"domain_name": "x"}, state)

    assert counter["count"] == 2, (
        f"Expected cidx_meta_read_path accessed once per call (total 2), got {counter['count']}"
    )


def test_get_domain_summary_handler_registered_in_registry() -> None:
    """depmap_get_domain_summary is present in HANDLER_REGISTRY after module import."""
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    assert "depmap_get_domain_summary" in HANDLER_REGISTRY
