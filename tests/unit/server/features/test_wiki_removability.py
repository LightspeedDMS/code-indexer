"""The wiki is a real, removable add-on (modular plan phase 4e, acceptance 3).

Three states of one deployment, each reached through the front doors a user
has -- MCP JSON-RPC ``POST /mcp``, the ``/wiki`` pages and their static
files, the ``/admin`` wiki actions, health, the config API and the database:

(a) ``wiki`` in ``enabled_features``: the feature's MCP tool, pages (behind
    the core's hybrid authentication, a browser sent to the login page),
    static files, admin actions, migration, config section and startup
    wiring (the cache invalidator on the core's repository events) are all
    there and work;
(b) installed but not enabled: the server starts with none of it, and the
    core offers no wiki link or control (search results, repository pages);
(c) the cidx-wiki distribution not importable at all (simulated uninstall,
    in a fresh interpreter): the server starts, the wiki is absent, and
    enabling it fails loud.
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
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.features import repository_events
from code_indexer.server.features.migrations import feature_schema_versions_sqlite
from code_indexer.server.features.registry import set_active_registry
from code_indexer.server.startup.feature_wiring import (
    load_configured_features,
    start_features,
    stop_features,
)
from tests.unit.server._isolated_app import isolated_app

pytestmark = pytest.mark.timeout(180)

PASSWORD = "Example-Wiki-Passw0rd!"
ADMIN = "example_admin"
USER = "example_user"
WIKI_TOOLS = {"wiki_article_analytics"}
STATIC_CSS = "/wiki/_static/wiki.css"
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
    everything, anyone else nothing."""

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
def wiki_on(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("wiki-on"), ["wiki"])


@pytest.fixture(scope="module")
def wiki_off(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("wiki-off"), [])


def _bearer(client: TestClient, username: str) -> Dict[str, str]:
    account = getattr(client.app, "state").user_manager.get_user(username)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def _mcp(client: TestClient, method: str, params: Any) -> Any:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=_bearer(client, ADMIN),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_names(client: TestClient) -> set:
    body = _mcp(client, "tools/list", {})
    return {tool["name"] for tool in body["result"]["tools"]}


def _health(client: TestClient) -> Dict[str, Any]:
    # The package conftest unpublishes the registry before every test; the
    # lifespan helper publishes it again, as the running server does.
    anyio.run(load_configured_features, client.app, None)
    response = client.get("/health", headers=_bearer(client, ADMIN))
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _db_versions() -> Dict[str, int]:
    from code_indexer.server.services.config_service import get_config_service

    data_dir = Path(get_config_service().config_manager.server_dir) / "data"
    return feature_schema_versions_sqlite(str(data_dir / "cidx_server.db"))


def _config_settings() -> Dict[str, Any]:
    from code_indexer.server.services.config_service import get_config_service

    settings: Dict[str, Any] = get_config_service().get_all_settings()
    return settings


# ── (a) enabled: the wiki works through every front door ─────────────────────


def test_enabled_lists_the_wiki_tool(wiki_on: SimpleNamespace) -> None:
    assert WIKI_TOOLS <= _tool_names(wiki_on.client)


def test_enabled_page_sends_a_browser_to_login(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    client.cookies.clear()
    response = client.get("/wiki/example-repo/getting-started")
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?redirect_to=/wiki/example-repo/getting-started"
    )


def test_enabled_search_answers_401_unauthenticated(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    client.cookies.clear()
    assert client.get("/wiki/example-repo/_search?q=hello").status_code == 401


def test_enabled_page_renders_for_an_authenticated_caller(
    wiki_on: SimpleNamespace, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "home.md").write_text("# Example Home\n\nWelcome.\n", encoding="utf-8")
    client = wiki_on.client
    from cidx_wiki.server.routes import _reset_wiki_cache

    # The wiki cache is a process singleton bound to the first app's
    # database: bind it to this app's (one app per process in production).
    _reset_wiki_cache()
    try:
        with patch(
            "cidx_wiki.server.routes._check_wiki_access", return_value=str(repo)
        ):
            response = client.get("/wiki/example-repo/", headers=_bearer(client, USER))
    finally:
        _reset_wiki_cache()
    assert response.status_code == 200, response.text
    assert "Welcome." in response.text
    assert STATIC_CSS in response.text


def test_enabled_invisible_repository_is_not_found(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    response = client.get("/wiki/example-repo/", headers=_bearer(client, USER))
    assert response.status_code == 404


def test_enabled_static_files_need_no_login(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    client.cookies.clear()
    response = client.get(STATIC_CSS)
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_enabled_admin_action_requires_manage_users(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    response = client.post(
        "/admin/golden-repos/example-repo/wiki-refresh",
        data={"csrf_token": "x"},
        headers=_bearer(client, USER),
    )
    assert response.status_code == 403


def test_enabled_migration_config_health_and_events(wiki_on: SimpleNamespace) -> None:
    client = wiki_on.client
    assert _db_versions().get("wiki") == 1
    listed = client.get(
        "/api/admin/features/config", headers=_bearer(client, ADMIN)
    ).json()
    (section,) = [s for s in listed["sections"] if s["feature"] == "wiki"]
    values = {key["name"]: key["value"] for key in section["keys"]}
    assert values["enable_views_seeding"] is True
    health = _health(client)["features"]
    loaded = {entry["name"]: entry for entry in health["loaded"]}
    assert loaded["wiki"]["started"] is True
    assert repository_events.subscriber_count() >= 1
    # Shown only in the generic Feature Settings block: no core category.
    assert "wiki_config" not in _config_settings()


def test_enabled_offers_wiki_links(wiki_on: SimpleNamespace) -> None:
    from code_indexer.server.features.registry import feature_offered

    anyio.run(load_configured_features, wiki_on.app, None)
    assert feature_offered("wiki") is True


# ── (b) installed but not enabled: no server side ────────────────────────────


def test_disabled_has_no_wiki_server_side(wiki_off: SimpleNamespace) -> None:
    client = wiki_off.client
    assert not WIKI_TOOLS & _tool_names(client)
    headers = _bearer(client, ADMIN)
    assert client.get("/wiki/example-repo/", headers=headers).status_code == 404
    assert client.get(STATIC_CSS).status_code == 404
    response = client.post(
        "/admin/golden-repos/example-repo/wiki-toggle",
        data={"wiki_enabled": "1"},
        headers=headers,
    )
    assert response.status_code in (404, 405)
    assert "wiki" not in _db_versions()
    health = _health(client)["features"]
    assert "wiki" not in {entry["name"] for entry in health["loaded"]}
    assert "wiki" in health["installed_not_enabled"]
    assert "wiki_config" not in _config_settings()


def test_disabled_core_offers_no_wiki_links(wiki_off: SimpleNamespace) -> None:
    from code_indexer.server.features.registry import feature_offered
    from code_indexer.server.mcp.handlers._utils import _get_wiki_enabled_repos

    anyio.run(load_configured_features, wiki_off.app, None)
    assert feature_offered("wiki") is False
    assert _get_wiki_enabled_repos() == set()


# ── (c) not importable: the server starts without it ─────────────────────────

_UNINSTALLED_PROBE = r"""
import importlib.abc, json, sys
from pathlib import Path

class _Uninstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cidx_wiki" or name.startswith("cidx_wiki."):
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
remove_entry_points(FEATURES_ENTRY_POINT_GROUP, ["wiki"])
withdraw_installed_distribution("cidx-wiki")  # where pip installed it

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
    out["page"] = client.get("/wiki/example-repo/").status_code
    out["static"] = client.get("/wiki/_static/wiki.css").status_code
    out["health"] = client.get("/health").status_code

config(["wiki"])
try:
    with isolated_app(root):
        pass
    out["enabled_refusal"] = None
except Exception as exc:
    out["enabled_refusal"] = type(exc).__name__
out["imported"] = sorted(m for m in sys.modules if m.startswith("cidx_wiki"))
print(json.dumps(out))
"""


def test_uninstalled_server_starts_without_the_wiki(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [entry for entry in sys.path if entry and "cidx-wiki" not in entry]
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
    assert "wiki" not in out["installed"]
    assert not WIKI_TOOLS & set(out["tools"])
    assert out["page"] == 404
    assert out["static"] == 404
    assert out["enabled_refusal"] == "FeatureNotInstalledError"
    assert out["imported"] == []
