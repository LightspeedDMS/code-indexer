"""Doors reach golden-repo, provider-index and configuration changes only
through audited entry points; internal maintenance stays unaudited.

The scan covers each door module's syntax tree (attribute access, bare-name
references and ``getattr`` lookups), so a spaced call, a saved reference
and a by-name lookup are all caught.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set

import code_indexer

_SERVER = Path(code_indexer.__file__).parent / "server"
_DOOR_FILES: List[Path] = sorted(
    [
        *(_SERVER / "routers").glob("*.py"),
        *(_SERVER / "web").glob("*.py"),
        *(_SERVER / "global_routes").glob("*.py"),
        *(_SERVER / "mcp" / "handlers").rglob("*.py"),
        _SERVER / "auth" / "oidc" / "routes.py",
        _SERVER / "auth" / "oauth" / "routes.py",
    ]
)

# Unaudited primitives a door must never call.
_UNAUDITED = frozenset(
    {
        "trigger_refresh_for_repo",
        "update_setting",
        "update_settings_atomic",
        "update_totp_elevation_atomic",
        "_apply_setting",
        "save_config",
        "save_token",
        "delete_token",
        "remove_orphaned_golden_repo",
        "_submit_add_job",
        "_submit_removal",
        "_submit_removal_job",
        "_submit_add_indexes_job",
        "_submit_branch_change",
        "_remove_provider_from_config",
    }
)
_AUDITED_ENTRY_POINTS = frozenset(
    {
        "add_golden_repo",
        "remove_golden_repo",
        "add_index_to_golden_repo",
        "add_indexes_to_golden_repo",
        "change_branch_async",
        "request_golden_repo_refresh",
        "submit_provider_scoped_index_job",
        "submit_provider_index_job",
        "remove_provider_index_audited",
        "bulk_add_provider_index_audited",
        "apply_audited_change",
        "update_settings_audited",
        "update_totp_elevation_audited",
        "reset_to_defaults_audited",
        "set_config",
        "save_token_audited",
        "delete_token_audited",
        "apply_git_settings_change",
    }
)
# Routine internal maintenance that must stay on the unaudited scheduler call.
_INTERNAL_REFRESH_CALLERS = [
    Path(code_indexer.__file__).parent / "global_repos" / "meta_description_hook.py",
    _SERVER / "services" / "dependency_map_service.py",
    _SERVER / "services" / "memory_store_service.py",
    _SERVER / "services" / "langfuse_trace_sync_service.py",
]


def _names(source: str) -> List[ast.AST]:
    found: List[ast.AST] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Attribute, ast.Name)):
            found.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.append(node.args[1])
    return found


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    assert isinstance(node, ast.Constant)
    return str(node.value)


def _uses(source: str, wanted: Set[str]) -> List[str]:
    return [
        f"{getattr(node, 'lineno', '?')}: {_name_of(node)}"
        for node in _names(source)
        if _name_of(node) in wanted
    ]


def _door_sources() -> Dict[Path, str]:
    return {path: path.read_text() for path in _DOOR_FILES}


def test_no_door_uses_an_unaudited_primitive() -> None:
    offenders = [
        f"{path.relative_to(_SERVER)}:{use}"
        for path, source in _door_sources().items()
        for use in _uses(source, set(_UNAUDITED))
    ]
    assert offenders == []


_REST_OPS = "routers/inline_admin_ops.py"
_MCP_REPOS = "mcp/handlers/repos.py"
_WEB = "web/routes.py"
_REST_PROVIDER = "routers/provider_indexes.py"

# capability -> {door module (relative to server/): audited entry point}
_DOORS_BY_CAPABILITY: Dict[str, Dict[str, str]] = {
    "golden_repo_added": {
        _REST_OPS: "add_golden_repo",
        _MCP_REPOS: "add_golden_repo",
        _WEB: "add_golden_repo",
    },
    "golden_repo_removed": {
        _REST_OPS: "remove_golden_repo",
        _MCP_REPOS: "remove_golden_repo",
        _WEB: "remove_golden_repo",
    },
    "golden_repo_refreshed": {
        _REST_OPS: "request_golden_repo_refresh",
        _MCP_REPOS: "request_golden_repo_refresh",
        _WEB: "request_golden_repo_refresh",
    },
    "golden_repo_index_added": {
        _REST_OPS: "add_indexes_to_golden_repo",
        _MCP_REPOS: "add_index_to_golden_repo",
    },
    "golden_repo_index_added (provider-scoped)": {
        _REST_OPS: "submit_provider_scoped_index_job",
    },
    "golden_repo_branch_changed": {
        _MCP_REPOS: "change_branch_async",
        _WEB: "change_branch_async",
    },
    "provider_index_added / recreated": {
        _REST_PROVIDER: "submit_provider_index_job",
        _MCP_REPOS: "submit_provider_index_job",
    },
    "provider_index_removed": {
        _REST_PROVIDER: "remove_provider_index_audited",
        _MCP_REPOS: "remove_provider_index_audited",
    },
    "provider_index_bulk_added": {
        _REST_PROVIDER: "bulk_add_provider_index_audited",
        _MCP_REPOS: "bulk_add_provider_index_audited",
    },
    "config_changed (web sections)": {_WEB: "update_settings_audited"},
    "config_changed (totp_elevation)": {_WEB: "update_totp_elevation_audited"},
    "config_changed (reset)": {_WEB: "reset_to_defaults_audited"},
    "config_changed (self-monitoring)": {_WEB: "apply_audited_change"},
    "config_changed (global config)": {
        "global_routes/routes.py": "set_config",
        "mcp/handlers/admin/__init__.py": "set_config",
    },
    "config_changed (llm-creds)": {"routers/llm_creds.py": "apply_audited_change"},
    "provider_api_key_set / cleared": {"routers/api_keys.py": "apply_audited_change"},
    "git_settings_changed": {
        "global_routes/git_settings.py": "apply_git_settings_change"
    },
    "ci_token_set": {_WEB: "save_token_audited"},
    "ci_token_deleted": {_WEB: "delete_token_audited"},
}


def _missing_door_wiring(
    doors_by_capability: Dict[str, Dict[str, str]], sources: Dict[str, str]
) -> List[str]:
    missing: List[str] = []
    for capability, doors in doors_by_capability.items():
        for door, entry_point in doors.items():
            names = {_name_of(node) for node in _names(sources[door])}
            if entry_point not in names:
                missing.append(f"{capability}: {door} lacks {entry_point}")
    return missing


def test_every_door_of_a_capability_uses_its_audited_entry_point() -> None:
    sources = {
        door: (_SERVER / door).read_text()
        for doors in _DOORS_BY_CAPABILITY.values()
        for door in doors
    }
    assert _missing_door_wiring(_DOORS_BY_CAPABILITY, sources) == []


def test_every_audited_entry_point_is_mapped_to_a_door() -> None:
    mapped = {ep for doors in _DOORS_BY_CAPABILITY.values() for ep in doors.values()}
    assert sorted(_AUDITED_ENTRY_POINTS - mapped) == []


def test_the_per_door_check_reports_a_door_missing_its_entry_point() -> None:
    """Negative control: one door without the call is reported, by name."""
    doors = {"refresh": {"a.py": "request_golden_repo_refresh", "b.py": "x"}}
    sources = {"a.py": "request_golden_repo_refresh(s, a, actor=u)", "b.py": "y()"}
    assert _missing_door_wiring(doors, sources) == ["refresh: b.py lacks x"]


def test_internal_refresh_callers_stay_unaudited() -> None:
    for path in _INTERNAL_REFRESH_CALLERS:
        source = path.read_text()
        assert _uses(source, {"trigger_refresh_for_repo"}), path
        assert _uses(source, {"request_golden_repo_refresh"}) == [], path


def test_the_reconciler_removes_orphans_as_the_system_component() -> None:
    source = (_SERVER / "services" / "golden_repo_reconciler.py").read_text()
    assert _uses(source, {"remove_orphaned_golden_repo"})
    assert _uses(source, {"remove_golden_repo"}) == []


def test_the_scan_detects_every_way_of_naming_a_primitive() -> None:
    """Negative controls: each spelling of a primitive use is flagged."""
    wanted = {"trigger_refresh_for_repo", "save_config"}
    assert _uses("s.trigger_refresh_for_repo(a)", wanted) == [
        "1: trigger_refresh_for_repo"
    ]
    assert _uses("fn = s.save_config\nfn(c)", wanted) == ["1: save_config"]
    assert _uses("getattr(s, 'save_config')(c)", wanted) == ["1: save_config"]
    assert _uses("request_golden_repo_refresh(s, a, actor=u)", wanted) == []
