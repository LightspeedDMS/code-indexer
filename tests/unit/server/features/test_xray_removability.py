"""X-Ray is a real, removable add-on (modular plan phase 4c, acceptance 3).

Three states of one deployment, each reached through the front doors a user
has -- MCP JSON-RPC ``POST /mcp``, the REST routes, the ``cidx`` CLI, health,
the config API and the database:

(a) ``xray`` in ``enabled_features``: the feature's tools, routes, migration,
    config section and startup wiring are all there and work;
(b) installed but not enabled: the server starts with none of the server
    side, the core's own tools (``cidx_fetch_cached_payload``, ``cancel_job``)
    are untouched, and ``cidx xray`` stays available (CLI by entry point);
(c) the cidx-xray distribution not importable at all (simulated uninstall, in
    a fresh interpreter): the server and the CLI start, X-Ray is absent, and
    enabling it fails loud.

No ``xray-cli`` binary is needed: ``xray_dump_ast`` parses in-process
(tree-sitter) and ``xray_search`` is checked up to its job submission on the
feature's own executor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import anyio
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from code_indexer import cli_features
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.features.migrations import feature_schema_versions_sqlite
from code_indexer.server.features.registry import set_active_registry
from code_indexer.server.startup.feature_wiring import (
    load_configured_features,
    start_features,
    stop_features,
)
from tests.unit.server._isolated_app import isolated_app

pytestmark = pytest.mark.timeout(180)

PASSWORD = "Example-Xray-Passw0rd!"
ADMIN = "example_admin"
USER = "example_user"
XRAY_TOOLS = {
    "analyze_graph",
    "store_xray_pattern",
    "xray_dump_ast",
    "xray_explore",
    "xray_search",
    "xray_search_batch",
}
CORE_TOOLS_THAT_STAY = {"cidx_fetch_cached_payload", "cancel_job"}
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
    with isolated_app(root) as app:
        app.state.access_filtering_service = _AdminOnlyAccess()
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
def xray_on(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("xray-on"), ["xray"])


@pytest.fixture(scope="module")
def xray_off(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("xray-off"), [])


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


# ── (a) enabled: X-Ray works through every front door ────────────────────────


def test_enabled_lists_every_xray_tool(xray_on: SimpleNamespace) -> None:
    names = _tool_names(xray_on.client)
    assert XRAY_TOOLS <= names
    assert CORE_TOOLS_THAT_STAY <= names


def test_enabled_xray_dump_ast_parses_through_mcp(
    xray_on: SimpleNamespace, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "example.py").write_text("def greet():\n    return 1\n", encoding="utf-8")
    with patch(
        "cidx_xray.server.mcp.xray._dump_ast._resolve_repo_path",
        return_value=str(repo),
    ):
        body = _call(
            xray_on.client,
            "xray_dump_ast",
            {"repository_alias": "example-global", "file_path": "example.py"},
        )
    payload = _tool_payload(body)
    assert payload.get("language") == "python", payload
    assert payload["ast_tree"]["type"] == "module"


def test_enabled_xray_search_runs_on_the_features_executor(
    xray_on: SimpleNamespace, tmp_path: Path
) -> None:
    """The job is submitted to the executor on_startup built and tracked by
    the core's job tracker (no xray-cli needed to get this far)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "example.py").write_text("x = 1\n", encoding="utf-8")
    with patch(
        "cidx_xray.server.mcp.xray._search._resolve_repo_path",
        return_value=str(repo),
    ):
        payload = _tool_payload(
            _call(
                xray_on.client,
                "xray_search",
                {
                    "repository_alias": "example-global",
                    "pattern": "x",
                    "search_target": "content",
                },
            )
        )
    assert "job_id" in payload, payload
    tracker = xray_on.app.state.job_tracker
    assert tracker.get_job(payload["job_id"]) is not None


def test_enabled_xray_search_validates_at_the_handler(xray_on: SimpleNamespace) -> None:
    payload = _tool_payload(
        _call(
            xray_on.client,
            "xray_search",
            {"repository_alias": "example-global", "pattern": "x"},
        )
    )
    assert payload["error"] == "invalid_search_target"


def test_enabled_rest_route_is_mounted_behind_core_auth(
    xray_on: SimpleNamespace,
) -> None:
    client = xray_on.client
    body = {
        "repository_alias": "example-global",
        "driver_regex": "x",
        "search_target": "content",
        "evaluator_code": "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    }
    client.cookies.clear()
    assert client.post("/api/xray/search", json=body).status_code == 401
    # A user without a grant for the repository: the route's own access check.
    response = client.post("/api/xray/search", json=body, headers=_bearer(client, USER))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error_code"] == "access_denied"


def test_enabled_migration_config_and_health(xray_on: SimpleNamespace) -> None:
    app, client = xray_on.app, xray_on.client
    versions = _db_versions(app)
    assert versions.get("xray") == 1
    listed = client.get(
        "/api/admin/features/config", headers=_bearer(client, ADMIN)
    ).json()
    assert "xray" in {section["feature"] for section in listed["sections"]}
    health = _health(client)["features"]
    assert [entry["name"] for entry in health["loaded"]] == ["xray"]
    assert health["loaded"][0]["started"] is True


def test_enabled_cli_lists_xray(xray_on: SimpleNamespace) -> None:
    result = _cli(xray_on.server, ["xray", "--help"])
    assert result.exit_code == 0, result.output
    assert "search" in result.output


# ── (b) installed but not enabled: no server side, core untouched ────────────


def test_disabled_has_no_xray_server_side(xray_off: SimpleNamespace) -> None:
    client = xray_off.client
    names = _tool_names(client)
    assert not XRAY_TOOLS & names
    assert CORE_TOOLS_THAT_STAY <= names
    assert "error" in _call(client, "xray_search", {"pattern": "x"})
    response = client.post("/api/xray/search", json={}, headers=_bearer(client, ADMIN))
    assert response.status_code == 404
    assert "xray" not in _db_versions(xray_off.app)
    health = _health(client)["features"]
    assert health["loaded"] == [] and "xray" in health["installed_not_enabled"]


def test_disabled_core_tool_still_works(xray_off: SimpleNamespace) -> None:
    payload = _tool_payload(
        _call(xray_off.client, "cidx_fetch_cached_payload", {"handle": "unknown"})
    )
    assert payload["success"] is False


def test_disabled_cli_keeps_cidx_xray(xray_off: SimpleNamespace) -> None:
    """The CLI side follows installation, not enabled_features."""
    result = _cli(xray_off.server, ["xray", "--help"])
    assert result.exit_code == 0, result.output


def _db_versions(app: Any) -> Dict[str, int]:
    from code_indexer.server.services.config_service import get_config_service

    data_dir = Path(get_config_service().config_manager.server_dir) / "data"
    return feature_schema_versions_sqlite(str(data_dir / "cidx_server.db"))


# ── (c) not importable: the server and the CLI start without it ──────────────

_UNINSTALLED_PROBE = r"""
import importlib.abc, json, os, sys
from pathlib import Path

class _Uninstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cidx_xray" or name.startswith("cidx_xray."):
            raise ImportError(f"No module named {name!r} (uninstalled)")
        return None

sys.meta_path.insert(0, _Uninstalled())

# A source-tree run registers what the in-tree distributions declare (as
# tests/conftest.py does); cidx-xray is then withdrawn, as uninstalling the
# distribution withdraws its entry point.
from cidx_sdk.entry_points import FEATURES_ENTRY_POINT_GROUP
from cidx_sdk.seams.resolver import remove_entry_points
from cidx_testkit.registry import (
    register_in_tree_entry_points,
    withdraw_installed_distribution,
)
register_in_tree_entry_points(Path.cwd())
remove_entry_points(FEATURES_ENTRY_POINT_GROUP, ["xray"])
withdraw_installed_distribution("cidx-xray")  # where pip installed it

import anyio, click
from fastapi.testclient import TestClient
from code_indexer.cli import cli
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
    out["route"] = TestClient(app).post("/api/xray/search", json={}).status_code

ctx = click.Context(cli)
out["cli_xray"] = cli.get_command(ctx, "xray") is not None
out["cli_has_query"] = cli.get_command(ctx, "query") is not None

config(["xray"])
try:
    with isolated_app(root):
        pass
    out["enabled_refusal"] = None
except Exception as exc:
    out["enabled_refusal"] = type(exc).__name__
out["imported"] = sorted(m for m in sys.modules if m.startswith("cidx_xray"))
print(json.dumps(out))
"""


def test_uninstalled_server_and_cli_start_without_xray(tmp_path: Path) -> None:
    env = dict(os.environ)
    # This interpreter's own import path (sources, site-packages) minus the
    # cidx-xray distribution: the probe also blocks any other copy.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [entry for entry in sys.path if entry and "cidx-xray" not in entry]
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
    assert "xray" not in out["installed"]
    assert not XRAY_TOOLS & set(out["tools"])
    assert CORE_TOOLS_THAT_STAY <= set(out["tools"])
    assert out["route"] == 404
    assert out["cli_xray"] is False
    assert out["cli_has_query"] is True
    assert out["enabled_refusal"] == "FeatureNotInstalledError"
    assert out["imported"] == []
