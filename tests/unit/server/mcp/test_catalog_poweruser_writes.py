"""Power users who see every repository may modify the catalog.

A ``powerusers`` member with no acting-user scope whose grants cover every
repository the registry names writes the catalog like an admin, through the
MCP and REST file write doors. When any repository is withheld from the
group they keep only the writes every restricted caller has (memories, X-Ray
patterns, the allowed dependency-map files), and full write returns once the
repository is granted back. An unreadable alias universe, normal users and
the analysis-agent principal keep the restricted write set. Real catalog on
disk, real FileCRUDService, registry and group store. Neutral names only.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.mcp.handlers import files as files_module
from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_USERNAME,
)
from code_indexer.server.services.constants import DEFAULT_GROUP_POWERUSERS
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    MEMORY_GLOBAL,
    REGISTERED_ALIASES,
    RESTRICTED_USER,
)

from ..routers.test_catalog_write_gate_rest import client  # noqa: F401
from .catalog_file_door_env import CATALOG_ALIAS, CatalogFileDoorEnv, mcp_data, user
from .test_catalog_write_gate import REFUSAL, env  # noqa: F401

POWER_USER = "fixture-power"
DESCRIPTION = f"{ALPHA}.md"
DOMAIN = "dependency-map/payments.md"
GRANTED = [name for name in REGISTERED_ALIASES if name != "cidx-meta"]


def _powerusers(door: CatalogFileDoorEnv) -> Any:
    gam = door.fx.group_manager
    group = gam.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
    assert group is not None
    return gam, group


@pytest.fixture
def power(env: CatalogFileDoorEnv) -> Iterator[CatalogFileDoorEnv]:  # noqa: F811
    gam, group = _powerusers(env)
    for name in GRANTED:
        gam.grant_repo_access(name, group.id, granted_by="test")
    for who in (POWER_USER, ANALYSIS_AGENT_USERNAME):
        gam.assign_user_to_group(who, group.id, assigned_by="test")
    yield env


def _hash(door: CatalogFileDoorEnv, path: str) -> str:
    return hashlib.sha256((door.fx.root / path).read_bytes()).hexdigest()


def _edit(door: CatalogFileDoorEnv, path: str, who: str = POWER_USER) -> Dict[str, Any]:
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": path,
        "old_string": ALPHA,
        "new_string": "renamed-sample",
        "content_hash": _hash(door, path),
        "replace_all": True,
    }
    with door.serving():
        return mcp_data(files_module.handle_edit_file(params, user(who)))


def _create_pattern(door: CatalogFileDoorEnv, name: str) -> Dict[str, Any]:
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": f"xray-patterns/__any__/{name}",
        "content": "name: n\n",
    }
    with door.serving():
        return mcp_data(files_module.handle_create_file(params, user(POWER_USER)))


@pytest.mark.parametrize("path", [DESCRIPTION, DOMAIN])
def test_full_visibility_power_user_edits_via_mcp(
    power: CatalogFileDoorEnv, path: str
) -> None:
    data = _edit(power, path)

    assert data["success"] is True, data
    assert "renamed-sample" in (power.fx.root / path).read_text()


@pytest.mark.parametrize("path", [DESCRIPTION, DOMAIN])
def test_full_visibility_power_user_edits_via_rest(
    power: CatalogFileDoorEnv,
    client: TestClient,  # noqa: F811
    path: str,
) -> None:
    from code_indexer.server.auth.user_manager import UserRole

    caller = user(POWER_USER, role=UserRole.POWER_USER)
    client.app.dependency_overrides[get_current_user] = lambda: caller  # type: ignore[attr-defined]
    body = {
        "old_string": ALPHA,
        "new_string": "renamed-sample",
        "content_hash": _hash(power, path),
        "replace_all": True,
    }

    response = client.patch(f"/api/v1/repos/{CATALOG_ALIAS}/files/{path}", json=body)

    assert response.status_code == 200, response.text
    assert "renamed-sample" in (power.fx.root / path).read_text()


def test_withheld_repository_restores_the_restricted_write_set(
    power: CatalogFileDoorEnv,
) -> None:
    gam, group = _powerusers(power)
    gam.revoke_repo_access(GRANTED[-1], group.id)

    for path in (DESCRIPTION, DOMAIN):
        assert _edit(power, path) == {"success": False, "error": REFUSAL}, path
    memory = f"memories/{MEMORY_GLOBAL}.md"
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": memory,
        "old_string": "fixture memory",
        "new_string": "fixture note",
        "content_hash": _hash(power, memory),
    }
    with power.serving():
        edited = mcp_data(files_module.handle_edit_file(params, user(POWER_USER)))
    assert edited["success"] is True, edited
    assert _create_pattern(power, "kept.yaml")["success"] is True

    gam.grant_repo_access(GRANTED[-1], group.id, granted_by="test")
    assert _edit(power, DESCRIPTION)["success"] is True


def test_normal_user_is_still_refused(power: CatalogFileDoorEnv) -> None:
    assert _edit(power, DESCRIPTION, RESTRICTED_USER) == {
        "success": False,
        "error": REFUSAL,
    }


def test_unreadable_alias_universe_refuses(power: CatalogFileDoorEnv) -> None:
    def failing_listing() -> Any:
        raise RuntimeError("registry unavailable")

    power.state.backend_registry = SimpleNamespace(
        global_repos=SimpleNamespace(list_repos=failing_listing)
    )

    assert _edit(power, DESCRIPTION) == {"success": False, "error": REFUSAL}


def test_analysis_agent_principal_cannot_write(power: CatalogFileDoorEnv) -> None:
    before = (power.fx.root / DESCRIPTION).read_bytes()

    data = _edit(power, DESCRIPTION, ANALYSIS_AGENT_USERNAME)

    assert data == {"success": False, "error": REFUSAL}
    assert (power.fx.root / DESCRIPTION).read_bytes() == before


def test_power_user_paths_are_identical_on_disk_after_refusal(
    power: CatalogFileDoorEnv,
) -> None:
    gam, group = _powerusers(power)
    gam.revoke_repo_access(GRANTED[0], group.id)
    before = {p: (power.fx.root / p).read_bytes() for p in (DESCRIPTION, DOMAIN)}

    for path in before:
        _edit(power, path)

    assert {p: (power.fx.root / p).read_bytes() for p in before} == before


def test_power_user_reaches_catalog_write_mode(power: CatalogFileDoorEnv) -> None:
    from datetime import datetime

    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp.protocol import _check_repository_access
    from code_indexer.server.mcp.tools import TOOL_REGISTRY
    from code_indexer.server.services.file_crud_service import file_crud_service

    caller = User(
        username=POWER_USER,
        password_hash="hash",
        role=UserRole.POWER_USER,
        created_at=datetime.now(),
    )
    tools = ["enter_write_mode", "exit_write_mode", "create_file", "edit_file"]
    for tool in tools + ["delete_file"]:
        assert caller.has_permission(TOOL_REGISTRY[tool]["required_permission"]), tool
    for tool, argument in (
        ("enter_write_mode", "repo_alias"),
        ("edit_file", "repository_alias"),
    ):
        _check_repository_access(
            {argument: CATALOG_ALIAS}, caller, tool, power.fx.access_service
        )
    assert file_crud_service.is_write_exception(CATALOG_ALIAS)
