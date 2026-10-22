"""Power users who see every repository may modify the catalog.

A ``powerusers`` member holding ``repository:write`` with no acting-user
scope, whose grants cover every repository the registry names (read fresh
for each decision), writes the catalog like an admin through the MCP and
REST file write doors. When any repository is withheld from the group, the
registry or the group cannot be read, or the caller lacks the write
permission, the caller keeps only the writes every restricted caller has
(memories, X-Ray patterns, the allowed dependency-map files) and every other
path gets the fixed refusal. Normal users and the analysis-agent principal
keep the restricted write set. Real catalog on disk, real FileCRUDService,
registry and group store. Neutral names only.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
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


def _caller(who: str = POWER_USER, role: Optional[UserRole] = None) -> User:
    """The power user holds repository:write (power_user role)."""
    if role is None and who == POWER_USER:
        role = UserRole.POWER_USER
    return user(who, role=role)


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


def _edit(
    door: CatalogFileDoorEnv, path: str, caller: Optional[User] = None
) -> Dict[str, Any]:
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": path,
        "old_string": ALPHA,
        "new_string": "renamed-sample",
        "content_hash": _hash(door, path),
        "replace_all": True,
    }
    with door.serving():
        return mcp_data(files_module.handle_edit_file(params, caller or _caller()))


def _delete(door: CatalogFileDoorEnv, path: str) -> Dict[str, Any]:
    params = {"repository_alias": CATALOG_ALIAS, "file_path": path}
    with door.serving():
        return mcp_data(files_module.handle_delete_file(params, _caller()))


def _create_pattern(door: CatalogFileDoorEnv, name: str) -> Dict[str, Any]:
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": f"xray-patterns/__any__/{name}",
        "content": "name: n\n",
    }
    with door.serving():
        return mcp_data(files_module.handle_create_file(params, _caller()))


REFUSED = {"success": False, "error": REFUSAL}


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
    caller = _caller()
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
        assert _edit(power, path) == REFUSED, path
    memory = f"memories/{MEMORY_GLOBAL}.md"
    params = {
        "repository_alias": CATALOG_ALIAS,
        "file_path": memory,
        "old_string": "fixture memory",
        "new_string": "fixture note",
        "content_hash": _hash(power, memory),
    }
    with power.serving():
        edited = mcp_data(files_module.handle_edit_file(params, _caller()))
    assert edited["success"] is True, edited
    assert _create_pattern(power, "kept.yaml")["success"] is True

    gam.grant_repo_access(GRANTED[-1], group.id, granted_by="test")
    assert _edit(power, DESCRIPTION)["success"] is True


def test_repository_registered_after_a_decision_is_never_writable_unseen(
    power: CatalogFileDoorEnv,
) -> None:
    assert _edit(power, DESCRIPTION)["success"] is True
    power.fx.registry.register_repo(
        alias_name="new-hidden-global",
        repo_name="new-hidden",
        repo_url=None,
        index_path="/golden/new-hidden",
    )

    assert _delete(power, DOMAIN) == REFUSED
    assert (power.fx.root / DOMAIN).exists()


def test_registry_failure_after_a_decision_refuses(
    power: CatalogFileDoorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _edit(power, DESCRIPTION)["success"] is True

    def unavailable() -> Any:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(power.fx.registry, "list_repos", unavailable)

    assert _edit(power, DESCRIPTION) == REFUSED


def test_powerusers_member_without_the_write_permission_is_refused(
    power: CatalogFileDoorEnv,
) -> None:
    lower = _caller(POWER_USER, role=UserRole.NORMAL_USER)

    assert _edit(power, DESCRIPTION, lower) == REFUSED


def test_group_read_failure_gets_the_fixed_refusal(
    power: CatalogFileDoorEnv,
    client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_username: str) -> Any:
        raise RuntimeError("group unavailable")

    monkeypatch.setattr(power.fx.group_manager, "get_user_group", unavailable)
    caller = _caller()
    client.app.dependency_overrides[get_current_user] = lambda: caller  # type: ignore[attr-defined]

    response = client.delete(f"/api/v1/repos/{CATALOG_ALIAS}/files/{DESCRIPTION}")

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": REFUSAL}
    assert _edit(power, DESCRIPTION) == REFUSED


def test_normal_user_is_still_refused(power: CatalogFileDoorEnv) -> None:
    assert _edit(power, DESCRIPTION, _caller(RESTRICTED_USER)) == REFUSED


def test_unreadable_alias_universe_refuses(power: CatalogFileDoorEnv) -> None:
    def failing_listing() -> Any:
        raise RuntimeError("registry unavailable")

    power.state.backend_registry = SimpleNamespace(
        global_repos=SimpleNamespace(list_repos=failing_listing)
    )

    assert _edit(power, DESCRIPTION) == REFUSED


def test_analysis_agent_principal_cannot_write(power: CatalogFileDoorEnv) -> None:
    before = (power.fx.root / DESCRIPTION).read_bytes()
    agent = _caller(ANALYSIS_AGENT_USERNAME, role=UserRole.POWER_USER)

    assert _edit(power, DESCRIPTION, agent) == REFUSED
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


RETIRED = "retired-sample.md"
DENIED_FOR_FULL_WRITER = [
    "README.md",
    RETIRED,
    "dependency-map/reports.md",
    "dependency-map/legacy.md",
    "dependency-map/_domains.json",
    "dependency-map/_activity.md",
    ".gitignore",
]


def _recorded(action: Any) -> Any:
    from .test_catalog_write_gate import _RECORDING

    _RECORDING.opened = []
    try:
        result = action()
    finally:
        opened = _RECORDING.opened
        del _RECORDING.opened
    return result, opened


@pytest.mark.parametrize("path", DENIED_FOR_FULL_WRITER)
def test_full_writer_is_refused_where_the_read_view_denies(
    power: CatalogFileDoorEnv, path: str
) -> None:
    root = power.fx.root
    (root / RETIRED).write_text("# retired-sample\n\nretired text\n")
    target = root / path
    before = target.read_bytes()
    word = before.decode().split()[-1]
    requests = [
        {
            "repository_alias": CATALOG_ALIAS,
            "file_path": path,
            "old_string": word,
            "new_string": word,
            "content_hash": content_hash,
        }
        for content_hash in ("0" * 64, hashlib.sha256(before).hexdigest())
    ]

    def attempts() -> list:
        with power.serving():
            edits = [
                mcp_data(files_module.handle_edit_file(params, _caller()))
                for params in requests
            ]
        return [*edits, _delete(power, path)]

    replies, opened = _recorded(attempts)

    assert replies == [REFUSED, REFUSED, REFUSED], replies
    # A domain document's DENY needs its membership (the dependency-map
    # model reads every domain document); every other path is decided by
    # name and opens no catalog file.
    name_decided = not path.startswith("dependency-map/") or "/_" in path
    if name_decided:
        assert [p for p in opened if p.startswith(str(root) + "/")] == []
    assert target.read_bytes() == before


def test_full_writer_decision_reads_the_registry_once_per_write(
    power: CatalogFileDoorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list = []
    real = power.fx.registry.list_repos

    def counted() -> Any:
        reads.append(1)
        return real()

    monkeypatch.setattr(power.fx.registry, "list_repos", counted)

    assert _edit(power, DESCRIPTION)["success"] is True
    assert len(reads) == 1
    reads.clear()
    assert _edit(power, ".git/config")["success"] is False
    assert len(reads) <= 1


def test_power_user_reaches_catalog_write_mode(power: CatalogFileDoorEnv) -> None:
    from code_indexer.server.mcp.protocol import _check_repository_access
    from code_indexer.server.mcp.tools import TOOL_REGISTRY
    from code_indexer.server.services.file_crud_service import file_crud_service

    caller = _caller()
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
