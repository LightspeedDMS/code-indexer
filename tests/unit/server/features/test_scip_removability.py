"""SCIP is a real, removable add-on (modular phase 4d).

Three states of one deployment, each reached through the front doors a user
has -- MCP JSON-RPC ``POST /mcp``, the REST routes, the query page's search
modes, the indexing pipeline's index types, the ``cidx`` CLI, health, the
config API and the database:

(a) ``scip`` in ``enabled_features``: the feature's tools, routes, search
    mode, index builder, migration, config section and startup wiring are
    all there and work;
(b) installed but not enabled: the server starts with none of the server
    side -- no SCIP index type for the pipeline, no ``scip`` search mode --
    the core's own tools are untouched, and ``cidx scip`` stays available
    (CLI by entry point);
(c) the cidx-scip distribution not importable at all (simulated uninstall, in
    a fresh interpreter): the server and the CLI start, SCIP is absent, and
    enabling it fails loud.

No SCIP indexer toolchain is needed: the queries run against an empty set of
golden repositories.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import anyio
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from code_indexer import cli_features
from code_indexer.kernel.index_builders import contributed_index_types, index_builder
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.features.migrations import feature_schema_versions_sqlite
from code_indexer.server.features.query_modes import feature_query_modes
from code_indexer.server.features.registry import set_active_registry
from code_indexer.server.startup.feature_wiring import (
    load_configured_features,
    start_features,
    stop_features,
)
from tests.unit.server._isolated_app import isolated_app

pytestmark = pytest.mark.timeout(180)

PASSWORD = "Example-Scip-Passw0rd!"
ADMIN = "example_admin"
USER = "example_user"
SCIP_TOOLS = {
    "scip_callchain",
    "scip_cleanup_history",
    "scip_cleanup_status",
    "scip_cleanup_workspaces",
    "scip_context",
    "scip_definition",
    "scip_dependencies",
    "scip_dependents",
    "scip_impact",
    "scip_pr_history",
    "scip_references",
}
CORE_TOOLS_THAT_STAY = {"cidx_fetch_cached_payload", "cancel_job", "search_code"}
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_server_config(root: Path, enabled: List[str]) -> Path:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "config.json").write_text(
        json.dumps({"server_dir": str(server), "enabled_features": enabled}),
        encoding="utf-8",
    )
    return server


class _AdminOnlyAccess:
    """The repository access decision without group grants: the admin reads
    everything, anyone else nothing (the lifespan that builds the real
    service is not run here)."""

    def is_admin_user(self, username: str) -> bool:
        return username == ADMIN

    def get_accessible_repos(self, username: str) -> set:
        return set()

    def caller_activation_sources(self, username: str) -> set:
        return set()

    def alias_granted(self, alias: str, accessible: set, sources: set) -> bool:
        return False


def _app(root: Path, enabled: List[str]) -> Iterator[SimpleNamespace]:
    server = _write_server_config(root, enabled)
    golden = root / "golden-repos"
    golden.mkdir()
    with isolated_app(root) as app:
        app.state.access_filtering_service = _AdminOnlyAccess()
        # The lifespan (not run here) sets it; no golden repository exists.
        app.state.golden_repos_dir = str(golden)
        accounts = app.state.user_manager
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        anyio.run(load_configured_features, app, None)
        anyio.run(start_features, app)
        try:
            yield SimpleNamespace(
                app=app, server=server, client=TestClient(app, follow_redirects=False)
            )
        finally:
            anyio.run(stop_features, app)
            set_active_registry(None)


@pytest.fixture(scope="module")
def scip_on(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("scip-on"), ["scip"])


@pytest.fixture(scope="module")
def scip_off(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("scip-off"), [])


def _bearer(client: TestClient, username: str) -> Dict[str, str]:
    account = getattr(client.app, "state").user_manager.get_user(username)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def _mcp(client: TestClient, username: str, method: str, params: Any) -> Any:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=_bearer(client, username),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_names(client: TestClient) -> set:
    body = _mcp(client, ADMIN, "tools/list", {})
    return {tool["name"] for tool in body["result"]["tools"]}


def _call(client: TestClient, tool: str, arguments: Dict[str, Any]) -> Any:
    body = _mcp(client, ADMIN, "tools/call", {"name": tool, "arguments": arguments})
    return body


def _tool_payload(body: Any) -> Dict[str, Any]:
    assert "result" in body, body
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _health(client: TestClient) -> Dict[str, Any]:
    # The package conftest unpublishes the registry before every test; the
    # lifespan helper publishes it again, as the running server does.
    anyio.run(load_configured_features, client.app, None)
    response = client.get("/health", headers=_bearer(client, ADMIN))
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _cli(server: Path, args: List[str]) -> Any:
    from code_indexer.cli import cli

    cli_features.reset_feature_commands()
    try:
        return CliRunner().invoke(cli, args, env={"CIDX_SERVER_DATA_DIR": str(server)})
    finally:
        cli_features.reset_feature_commands()


def _db_versions(app: Any) -> Dict[str, int]:
    from code_indexer.server.services.config_service import get_config_service

    data_dir = Path(get_config_service().config_manager.server_dir) / "data"
    return feature_schema_versions_sqlite(str(data_dir / "cidx_server.db"))


# ── (a) enabled: SCIP works through every front door ─────────────────────────


def test_enabled_lists_every_scip_tool(scip_on: SimpleNamespace) -> None:
    names = _tool_names(scip_on.client)
    assert SCIP_TOOLS <= names
    assert CORE_TOOLS_THAT_STAY <= names


def test_enabled_scip_definition_answers_through_mcp(
    scip_on: SimpleNamespace,
) -> None:
    """The feature's handler runs behind the core's dispatcher: no golden
    repository holds a SCIP index, so the query succeeds with nothing."""
    payload = _tool_payload(_call(scip_on.client, "scip_definition", {"symbol": "x"}))
    assert payload["success"] is True, payload
    assert payload["results"] == []


def test_enabled_rest_routes_are_mounted_behind_core_auth(
    scip_on: SimpleNamespace,
) -> None:
    client = scip_on.client
    client.cookies.clear()
    assert client.get("/scip/definition", params={"symbol": "x"}).status_code == 401
    response = client.get(
        "/scip/definition", params={"symbol": "x"}, headers=_bearer(client, ADMIN)
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    # The admin workspace-cleanup routes require the administrators' permission.
    status = client.get(
        "/api/admin/scip-cleanup-status", headers=_bearer(client, ADMIN)
    )
    assert status.status_code == 200, status.text
    forbidden = client.get(
        "/api/admin/scip-cleanup-status", headers=_bearer(client, USER)
    )
    assert forbidden.status_code == 403


def test_enabled_pipeline_and_query_page_know_scip(scip_on: SimpleNamespace) -> None:
    builder = index_builder("scip")
    assert builder is not None
    assert "scip" in contributed_index_types()
    assert builder.builds_locally is not None and builder.builds_locally() is True
    assert "scip" in feature_query_modes()


def test_enabled_migration_config_and_health(scip_on: SimpleNamespace) -> None:
    app, client = scip_on.app, scip_on.client
    assert _db_versions(app).get("scip") == 1
    listed = client.get(
        "/api/admin/features/config", headers=_bearer(client, ADMIN)
    ).json()
    assert "scip" in {section["feature"] for section in listed["sections"]}
    health = _health(client)["features"]
    assert [entry["name"] for entry in health["loaded"]] == ["scip"]
    assert health["loaded"][0]["started"] is True


def test_enabled_cli_lists_scip(scip_on: SimpleNamespace) -> None:
    result = _cli(scip_on.server, ["scip", "--help"])
    assert result.exit_code == 0, result.output
    assert "generate" in result.output


# ── (b) installed but not enabled: no server side, core untouched ────────────


def test_disabled_has_no_scip_server_side(scip_off: SimpleNamespace) -> None:
    client = scip_off.client
    names = _tool_names(client)
    assert not SCIP_TOOLS & names
    assert CORE_TOOLS_THAT_STAY <= names
    assert "error" in _call(client, "scip_definition", {"symbol": "x"})
    response = client.get(
        "/scip/definition", params={"symbol": "x"}, headers=_bearer(client, ADMIN)
    )
    assert response.status_code == 404
    status = client.get(
        "/api/admin/scip-cleanup-status", headers=_bearer(client, ADMIN)
    )
    assert status.status_code == 404
    assert "scip" not in _db_versions(scip_off.app)
    health = _health(client)["features"]
    assert health["loaded"] == [] and "scip" in health["installed_not_enabled"]


def test_disabled_pipeline_builds_no_scip(scip_off: SimpleNamespace) -> None:
    """No index type for the pipeline, no search mode for the query page."""
    assert index_builder("scip") is None
    assert "scip" not in contributed_index_types()
    assert "scip" not in feature_query_modes()


def test_disabled_core_tool_still_works(scip_off: SimpleNamespace) -> None:
    payload = _tool_payload(
        _call(scip_off.client, "cidx_fetch_cached_payload", {"handle": "unknown"})
    )
    assert payload["success"] is False


def test_disabled_cli_keeps_cidx_scip(scip_off: SimpleNamespace) -> None:
    """The CLI side follows installation, not enabled_features."""
    result = _cli(scip_off.server, ["scip", "--help"])
    assert result.exit_code == 0, result.output


# ── (c) not importable: the server and the CLI start without it ──────────────

_UNINSTALLED_PROBE = r"""
import importlib.abc, json, os, sys
from pathlib import Path

class _Uninstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cidx_scip" or name.startswith("cidx_scip."):
            raise ImportError(f"No module named {name!r} (uninstalled)")
        return None

sys.meta_path.insert(0, _Uninstalled())

# A source-tree run registers what the in-tree distributions declare (as
# tests/conftest.py does); cidx-scip is then withdrawn, as uninstalling the
# distribution withdraws its entry point.
from cidx_sdk.entry_points import FEATURES_ENTRY_POINT_GROUP
from cidx_sdk.seams.resolver import remove_entry_points
from cidx_testkit.registry import (
    register_in_tree_entry_points,
    withdraw_installed_distribution,
)
register_in_tree_entry_points(Path.cwd())
remove_entry_points(FEATURES_ENTRY_POINT_GROUP, ["scip"])
withdraw_installed_distribution("cidx-scip")  # where pip installed it

import anyio, click
from fastapi.testclient import TestClient
from code_indexer.cli import cli
from code_indexer.kernel.index_builders import index_builder
from code_indexer.server.features.loader import discover_installed_features
from code_indexer.server.startup.feature_wiring import load_configured_features
from tests.unit.server._isolated_app import isolated_app

root = Path(sys.argv[1])
out = {"installed": sorted(discover_installed_features())}

def config(enabled):
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "config.json").write_text(
        json.dumps({"server_dir": str(server), "enabled_features": enabled})
    )

config([])
with isolated_app(root) as app:
    anyio.run(load_configured_features, app, None)
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
    from code_indexer.server.mcp.tools import TOOL_REGISTRY
    out["tools"] = sorted(set(TOOL_REGISTRY) | set(HANDLER_REGISTRY))
    client = TestClient(app)
    out["route"] = client.get("/scip/definition", params={"symbol": "x"}).status_code
    out["multi_route"] = client.post("/api/scip/multi/definition", json={}).status_code
    out["builder"] = index_builder("scip") is not None

ctx = click.Context(cli)
out["cli_scip"] = cli.get_command(ctx, "scip") is not None
out["cli_has_query"] = cli.get_command(ctx, "query") is not None

config(["scip"])
try:
    with isolated_app(root):
        pass
    out["enabled_refusal"] = None
except Exception as exc:
    out["enabled_refusal"] = type(exc).__name__
out["imported"] = sorted(m for m in sys.modules if m.startswith("cidx_scip"))
print(json.dumps(out))
"""


def test_uninstalled_server_and_cli_start_without_scip(tmp_path: Path) -> None:
    env = dict(os.environ)
    # This interpreter's own import path (sources, site-packages) minus the
    # cidx-scip distribution: the probe also blocks any other copy.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [entry for entry in sys.path if entry and "cidx-scip" not in entry]
    )
    result = subprocess.run(
        [sys.executable, "-c", _UNINSTALLED_PROBE, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=150,
        env=env,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-4000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert "scip" not in out["installed"]
    assert not SCIP_TOOLS & set(out["tools"])
    assert CORE_TOOLS_THAT_STAY <= set(out["tools"])
    assert out["route"] == 404
    assert out["multi_route"] == 404
    assert out["builder"] is False
    assert out["cli_scip"] is False
    assert out["cli_has_query"] is True
    assert out["enabled_refusal"] == "FeatureNotInstalledError"
    assert out["imported"] == []
