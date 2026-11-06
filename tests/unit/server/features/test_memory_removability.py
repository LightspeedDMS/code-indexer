"""The memory store is a real, removable add-on (modular plan phase 4f,
acceptance 3).

Three states of one deployment, each reached through the front doors a user
has -- MCP JSON-RPC ``POST /mcp``, the ``/api/v1/memories`` REST routes,
health, the config API and the search section registry:

(a) ``memory`` in ``enabled_features``: the feature's MCP tools and REST
    routes (behind the core's authentication), config section, startup
    wiring (the memory store over the catalog) and the ``relevant_memories``
    search section are all there, and a memory is written to the catalog;
(b) installed but not enabled: the server starts with none of it, and
    ``search_code`` answers no ``relevant_memories``;
(c) the cidx-memory distribution not importable at all (simulated uninstall,
    in a fresh interpreter): the server starts, the memory store is absent,
    and enabling it fails loud.

The catalog's memory files and the rule that decides who may see one stay
in the core (the file doors serve them whether or not the feature is
installed); they are not under test here.
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

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.features.registry import set_active_registry
from code_indexer.server.features.search_augmenters import active_search_augmenters
from code_indexer.server.startup.feature_wiring import (
    load_configured_features,
    start_features,
    stop_features,
)
from tests.unit.server._isolated_app import isolated_app

pytestmark = pytest.mark.timeout(180)

PASSWORD = "Example-Memory-Passw0rd!"
ADMIN = "example_admin"
USER = "example_user"
MEMORY_TOOLS = {"create_memory", "edit_memory", "delete_memory"}
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MEMORY = {
    "type": "gotcha",
    "scope": "global",
    "summary": "Example summary of a fact",
    "evidence": [{"file": "src/example.py", "lines": "1-2"}],
}


def _write_server_config(root: Path, enabled: List[str]) -> Path:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "config.json").write_text(
        json.dumps({"server_dir": str(server), "enabled_features": enabled}),
        encoding="utf-8",
    )
    return server


class _Scheduler:
    """The refresh scheduler's cidx-meta write lock and refresh trigger."""

    def __init__(self) -> None:
        self.refreshes: List[str] = []

    def acquire_write_lock(self, alias: str, owner_name: str, *args: Any) -> bool:
        return True

    def release_write_lock(self, alias: str, owner_name: str, *args: Any) -> bool:
        return True

    def is_write_locked(self, alias: str) -> bool:
        return False

    def trigger_refresh_for_repo(self, alias_name: str, *args: Any, **kw: Any) -> None:
        self.refreshes.append(alias_name)


class _Debouncer:
    def signal_dirty(self) -> None:
        return None


def _app(root: Path, enabled: List[str]) -> Iterator[SimpleNamespace]:
    server = _write_server_config(root, enabled)
    golden = root / "data" / "golden-repos"
    with isolated_app(root) as app:
        accounts = app.state.user_manager
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        # The catalog refresh wiring the lifespan builds, which the memory
        # store writes through.
        scheduler = _Scheduler()
        app.state.golden_repos_dir = str(golden)
        app.state.server_data_dir = str(root)
        app.state.cidx_meta_debouncer = _Debouncer()
        app.state.global_lifecycle_manager = SimpleNamespace(
            refresh_scheduler=scheduler
        )
        anyio.run(load_configured_features, app, None)
        anyio.run(start_features, app)
        try:
            yield SimpleNamespace(
                app=app,
                server=server,
                golden=golden,
                scheduler=scheduler,
                client=TestClient(app, follow_redirects=False),
            )
        finally:
            anyio.run(stop_features, app)
            set_active_registry(None)


@pytest.fixture(scope="module")
def memory_on(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("memory-on"), ["memory"])


@pytest.fixture(scope="module")
def memory_off(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    yield from _app(tmp_path_factory.mktemp("memory-off"), [])


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


def _config_settings() -> Dict[str, Any]:
    from code_indexer.server.services.config_service import get_config_service

    settings: Dict[str, Any] = get_config_service().get_all_settings()
    return settings


def _memory_files(golden: Path) -> List[str]:
    memories = golden / "cidx-meta" / "memories"
    return sorted(p.name for p in memories.glob("*.md")) if memories.is_dir() else []


# ── (a) enabled: memories work through every front door ──────────────────────


def test_enabled_lists_the_memory_tools(memory_on: SimpleNamespace) -> None:
    assert MEMORY_TOOLS <= _tool_names(memory_on.client)


def test_enabled_rest_route_requires_authentication(
    memory_on: SimpleNamespace,
) -> None:
    client = memory_on.client
    client.cookies.clear()
    assert client.post("/api/v1/memories", json=_MEMORY).status_code == 401


def test_enabled_rest_route_writes_a_memory_to_the_catalog(
    memory_on: SimpleNamespace,
) -> None:
    client = memory_on.client
    before = _memory_files(memory_on.golden)
    response = client.post(
        "/api/v1/memories", json=_MEMORY, headers=_bearer(client, USER)
    )
    assert response.status_code == 201, response.text
    written = response.json()["id"]
    assert f"{written}.md" in set(_memory_files(memory_on.golden)) - set(before)
    assert "cidx-meta-global" in memory_on.scheduler.refreshes


def test_enabled_mcp_tool_writes_a_memory(memory_on: SimpleNamespace) -> None:
    body = _mcp(
        memory_on.client, "tools/call", {"name": "create_memory", "arguments": _MEMORY}
    )
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True, payload
    assert f"{payload['id']}.md" in _memory_files(memory_on.golden)


def test_enabled_config_health_and_search_section(memory_on: SimpleNamespace) -> None:
    client = memory_on.client
    listed = client.get(
        "/api/admin/features/config", headers=_bearer(client, ADMIN)
    ).json()
    (section,) = [s for s in listed["sections"] if s["feature"] == "memory"]
    values = {key["name"]: key["value"] for key in section["keys"]}
    assert values["memory_retrieval_enabled"] is True
    health = _health(client)["features"]
    loaded = {entry["name"]: entry for entry in health["loaded"]}
    assert loaded["memory"]["started"] is True
    # Shown only in the generic Feature Settings block: no core category.
    assert "memory_retrieval" not in _config_settings()
    assert [a.key for a in active_search_augmenters("semantic")] == [
        "relevant_memories"
    ]
    assert active_search_augmenters("fts") == ()


# ── (b) installed but not enabled: no server side ────────────────────────────


def test_disabled_has_no_memory_server_side(memory_off: SimpleNamespace) -> None:
    client = memory_off.client
    assert not MEMORY_TOOLS & _tool_names(client)
    response = client.post(
        "/api/v1/memories", json=_MEMORY, headers=_bearer(client, USER)
    )
    assert response.status_code in (404, 405)
    assert _memory_files(memory_off.golden) == []
    health = _health(client)["features"]
    assert "memory" not in {entry["name"] for entry in health["loaded"]}
    assert "memory" in health["installed_not_enabled"]
    assert "memory_retrieval" not in _config_settings()
    assert active_search_augmenters("semantic") == ()


# ── (c) not importable: the server starts without it ─────────────────────────

_UNINSTALLED_PROBE = r"""
import importlib.abc, json, sys
from pathlib import Path

class _Uninstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "cidx_memory" or name.startswith("cidx_memory."):
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
remove_entry_points(FEATURES_ENTRY_POINT_GROUP, ["memory"])
withdraw_installed_distribution("cidx-memory")  # where pip installed it

import anyio
from fastapi.testclient import TestClient
from code_indexer.server.features.loader import discover_installed_features
from code_indexer.server.features.search_augmenters import active_search_augmenters
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
    out["route"] = client.post("/api/v1/memories", json={}).status_code
    out["health"] = client.get("/health").status_code
    out["sections"] = [a.key for a in active_search_augmenters("semantic")]

config(["memory"])
try:
    with isolated_app(root):
        pass
    out["enabled_refusal"] = None
except Exception as exc:
    out["enabled_refusal"] = type(exc).__name__
out["imported"] = sorted(m for m in sys.modules if m.startswith("cidx_memory"))
print(json.dumps(out))
"""


def test_uninstalled_server_starts_without_the_memory_store(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [entry for entry in sys.path if entry and "cidx-memory" not in entry]
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
    assert "memory" not in out["installed"]
    assert not MEMORY_TOOLS & set(out["tools"])
    assert out["route"] in (404, 405)
    assert out["sections"] == []
    assert out["enabled_refusal"] == "FeatureNotInstalledError"
    assert out["imported"] == []
