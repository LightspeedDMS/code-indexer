"""The dependency map is a real, removable add-on (modular plan phase 4g,
acceptance 3).

Three states of one deployment, each reached through the front doors a user
has -- MCP JSON-RPC ``POST /mcp``, the ``/admin/dependency-map`` page and its
static script, the ``/admin`` dependency-map routes, health, the config API
and the database:

(a) ``depmap`` in ``enabled_features``: the feature's MCP tools, page (a
    browser without a session is sent to the login page), static script,
    partials (401 without a session), migration, config section and startup
    wiring (the service in the feature's own slot -- never on the app state,
    which the core owns -- and the description fact check in the kernel
    slot) are all there;
(b) installed but not enabled: the server starts with none of it, and the
    core offers no dependency-map link or settings;
(c) the cidx-depmap distribution not importable at all (simulated uninstall,
    in a fresh interpreter): the server starts, the dependency map is absent,
    and enabling it fails loud.

The catalog's dependency-map files, their access projection and the
dependency-map tables stay in the core; they are not under test here.
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
from fastapi.testclient import TestClient

from code_indexer.kernel.capabilities import DESCRIPTION_FACT_CHECK_HOST
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

PASSWORD = "Example-DepMap-Passw0rd!"
ADMIN = "example_admin"
DEPMAP_TOOLS = {
    "depmap_find_consumers",
    "depmap_get_cross_domain_graph",
    "depmap_get_domain_summary",
    "depmap_get_hub_domains",
    "depmap_get_repo_domains",
    "depmap_get_stale_domains",
    "trigger_dependency_analysis",
}
STATIC_JS = "/admin/dependency-map/_static/dependency_map.js"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_server_config(root: Path, enabled: List[str]) -> Path:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "config.json").write_text(
        json.dumps({"server_dir": str(server), "enabled_features": enabled}),
        encoding="utf-8",
    )
    return server


def _app(root: Path, enabled: List[str]) -> Iterator[SimpleNamespace]:
    _write_server_config(root, enabled)
    with isolated_app(root) as app:
        app.state.user_manager.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        anyio.run(load_configured_features, app, None)
        anyio.run(start_features, app)
        try:
            yield SimpleNamespace(
                app=app, client=TestClient(app, follow_redirects=False)
            )
        finally:
            anyio.run(stop_features, app)
            set_active_registry(None)


@pytest.fixture(scope="module")
def depmap_on(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("depmap-on"), ["depmap"])


@pytest.fixture(scope="module")
def depmap_off(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("depmap-off"), [])


def _bearer(client: TestClient) -> Dict[str, str]:
    account = getattr(client.app, "state").user_manager.get_user(ADMIN)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": ADMIN, "role": account.role.value})
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def _tool_names(client: TestClient) -> set:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=_bearer(client),
    )
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def _health(client: TestClient) -> Dict[str, Any]:
    anyio.run(load_configured_features, client.app, None)
    response = client.get("/health", headers=_bearer(client))
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _db_versions() -> Dict[str, int]:
    from code_indexer.server.services.config_service import get_config_service

    data_dir = Path(get_config_service().config_manager.server_dir) / "data"
    return feature_schema_versions_sqlite(str(data_dir / "cidx_server.db"))


def _claude_cli_settings() -> Dict[str, Any]:
    from code_indexer.server.services.config_service import get_config_service

    settings: Dict[str, Any] = get_config_service().get_all_settings()["claude_cli"]
    return settings


# ── (a) enabled ───────────────────────────────────────────────────────────────


def test_enabled_lists_the_depmap_tools(depmap_on: SimpleNamespace) -> None:
    assert DEPMAP_TOOLS <= _tool_names(depmap_on.client)


def test_enabled_page_sends_a_browser_to_login(depmap_on: SimpleNamespace) -> None:
    client = depmap_on.client
    client.cookies.clear()
    response = client.get("/admin/dependency-map")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_enabled_partials_answer_401_unauthenticated(
    depmap_on: SimpleNamespace,
) -> None:
    client = depmap_on.client
    client.cookies.clear()
    assert client.get("/admin/partials/depmap-job-status").status_code == 401


def _nav_paths(app: Any, monkeypatch: pytest.MonkeyPatch, *, admin: bool) -> set:
    from code_indexer.server.features import web

    role = UserRole.ADMIN if admin else UserRole.NORMAL_USER
    monkeypatch.setattr(web, "_viewer_role", lambda request: role)
    return {entry["path"] for entry in app.state.feature_nav(object())}


def test_enabled_page_is_in_the_admin_nav_only(
    depmap_on: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = depmap_on.app
    assert "/admin/dependency-map" in _nav_paths(app, monkeypatch, admin=True)
    assert "/admin/dependency-map" not in _nav_paths(app, monkeypatch, admin=False)


def test_enabled_static_script_needs_no_login(depmap_on: SimpleNamespace) -> None:
    client = depmap_on.client
    client.cookies.clear()
    assert client.get(STATIC_JS).status_code == 200


def test_enabled_migration_config_health_and_wiring(
    depmap_on: SimpleNamespace,
) -> None:
    client = depmap_on.client
    assert _db_versions().get("depmap") == 1
    listed = client.get("/api/admin/features/config", headers=_bearer(client)).json()
    (section,) = [s for s in listed["sections"] if s["feature"] == "depmap"]
    values = {key["name"]: key["value"] for key in section["keys"]}
    assert values["dependency_map_enabled"] is False
    loaded = {e["name"]: e for e in _health(client)["features"]["loaded"]}
    assert loaded["depmap"]["started"] is True
    assert DESCRIPTION_FACT_CHECK_HOST.get() is not None
    assert "dependency_map_service" not in depmap_on.app.state._state
    # Shown only in the generic Feature Settings block, not the Claude CLI one.
    assert "dependency_map_enabled" not in _claude_cli_settings()


# ── (b) installed but not enabled ────────────────────────────────────────────


def test_disabled_has_no_depmap_server_side(depmap_off: SimpleNamespace) -> None:
    client = depmap_off.client
    assert not DEPMAP_TOOLS & _tool_names(client)
    headers = _bearer(client)
    assert client.get("/admin/dependency-map", headers=headers).status_code == 404
    assert client.get(STATIC_JS).status_code == 404
    assert "depmap" not in _db_versions()
    health = _health(client)["features"]
    assert "depmap" not in {entry["name"] for entry in health["loaded"]}
    assert "depmap" in health["installed_not_enabled"]
    assert "dependency_map_enabled" not in _claude_cli_settings()
    assert "dependency_map_service" not in depmap_off.app.state._state


def test_core_never_names_the_dependency_map_service() -> None:
    """The running service is the feature's (``runtime.SERVICE``); the core
    reaches nothing of it by name -- what it needs from the feature goes
    through kernel slots (``DESCRIPTION_FACT_CHECK_HOST``) and generic
    registrations (cancel handlers)."""
    core = _REPO_ROOT / "src" / "code_indexer"
    naming = [
        str(path.relative_to(_REPO_ROOT))
        for path in sorted(core.rglob("*.py"))
        if "dependency_map_service" in path.read_text(encoding="utf-8")
    ]
    assert naming == []


def test_disabled_core_offers_no_depmap_link(depmap_off: SimpleNamespace) -> None:
    from code_indexer.server.features.registry import feature_offered

    anyio.run(load_configured_features, depmap_off.app, None)
    assert feature_offered("depmap") is False
    nav = depmap_off.app.state.feature_nav
    assert "/admin/dependency-map" not in {entry["path"] for entry in nav(object())}


# ── (c) not importable ───────────────────────────────────────────────────────

_UNINSTALLED_PROBE = r"""
import importlib.abc, json, sys
from pathlib import Path

class _Uninstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cidx_depmap" or name.startswith("cidx_depmap."):
            raise ImportError(f"No module named {name!r} (uninstalled)")
        return None

sys.meta_path.insert(0, _Uninstalled())

from cidx_sdk.entry_points import FEATURES_ENTRY_POINT_GROUP
from cidx_sdk.seams.resolver import remove_entry_points
from cidx_testkit.registry import (
    register_in_tree_entry_points,
    withdraw_installed_distribution,
)
register_in_tree_entry_points(Path.cwd())
remove_entry_points(FEATURES_ENTRY_POINT_GROUP, ["depmap"])
withdraw_installed_distribution("cidx-depmap")  # where pip installed it

import anyio
from fastapi.testclient import TestClient
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
    client = TestClient(app, follow_redirects=False)
    out["page"] = client.get("/admin/dependency-map").status_code
    out["static"] = client.get("/admin/dependency-map/_static/dependency_map.js").status_code
    out["health"] = client.get("/health").status_code

config(["depmap"])
try:
    with isolated_app(root):
        pass
    out["enabled_refusal"] = None
except Exception as exc:
    out["enabled_refusal"] = type(exc).__name__
out["imported"] = sorted(m for m in sys.modules if m.startswith("cidx_depmap"))
print(json.dumps(out))
"""


def test_uninstalled_server_starts_without_the_dependency_map(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [entry for entry in sys.path if entry and "cidx-depmap" not in entry]
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
    assert "depmap" not in out["installed"]
    assert not DEPMAP_TOOLS & set(out["tools"])
    assert out["page"] in (401, 404)
    assert out["static"] == 404
    assert out["enabled_refusal"] == "FeatureNotInstalledError"
    assert out["imported"] == []
