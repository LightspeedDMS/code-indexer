"""X-Ray patterns written through the catalog file tools follow the
store_xray_pattern rule.

On the catalog in write mode a caller who is not an admin by role may not
replace (edit) or delete a shared ``__any__`` pattern, through MCP or REST,
whatever their group; a repository-scoped pattern needs a grant on that
repository; a new shared pattern may still be created; admins keep full
write. A refusal writes nothing. Real catalog on disk, real
FileCRUDService, write mode, registry and group store. Neutral names only.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files as files_module
from code_indexer.server.services import file_crud_service as crud_module
from code_indexer.server.services.cidx_meta_access.file_doors import UNRESTRICTED
from code_indexer.server.services.file_crud_service import file_crud_service
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    GAMMA,
    RESTRICTED_USER,
)

from ..routers.test_catalog_write_gate_rest import client  # noqa: F401
from .catalog_file_door_env import CATALOG_ALIAS, CatalogFileDoorEnv, mcp_data, user
from .test_catalog_write_gate import REFUSAL, env  # noqa: F401
from .test_catalog_poweruser_writes import POWER_USER, power  # noqa: F401

SHARED = "xray-patterns/__any__/p.yaml"
SHARED_REFUSAL = "Replacing or deleting a shared X-Ray pattern requires the admin role."
GRANTED_SCOPED = f"xray-patterns/{ALPHA}/scoped.yaml"
UNGRANTED_SCOPED = f"xray-patterns/{GAMMA}/scoped.yaml"
BASE = f"/api/v1/repos/{CATALOG_ALIAS}/files"


def _writer() -> User:
    """A non-admin holding repository:write, restricted to its grants."""
    return user(RESTRICTED_USER, role=UserRole.POWER_USER)


def _admin() -> User:
    return user(ADMIN_USER)


def _admins_group_power_user() -> User:
    """In the admins group (unrestricted catalog view) without the admin role."""
    return user(ADMIN_USER, role=UserRole.POWER_USER)


def _hash(door: CatalogFileDoorEnv, path: str) -> str:
    return hashlib.sha256((door.fx.root / path).read_bytes()).hexdigest()


def _put(door: CatalogFileDoorEnv, path: str, text: str = "name: scoped\n") -> None:
    target = door.fx.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _mcp(
    door: CatalogFileDoorEnv, tool: str, path: str, caller: User
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"repository_alias": CATALOG_ALIAS, "file_path": path}
    if tool == "create":
        params["content"] = "name: created\n"
        handler = files_module.handle_create_file
    elif tool == "edit":
        params.update(
            old_string=(door.fx.root / path).read_text(encoding="utf-8"),
            new_string="name: replaced\n",
            content_hash=_hash(door, path),
        )
        handler = files_module.handle_edit_file
    else:
        handler = files_module.handle_delete_file
    with door.serving():
        return mcp_data(handler(params, caller))


def _rest(
    door: CatalogFileDoorEnv,
    test_client: TestClient,
    verb: str,
    path: str,
    caller: User,
) -> Any:
    test_client.app.dependency_overrides[get_current_user] = lambda: caller  # type: ignore[attr-defined]
    if verb == "create":
        body = {"file_path": path, "content": "name: created\n"}
        return test_client.post(BASE, json=body)
    if verb == "edit":
        body = {
            "old_string": (door.fx.root / path).read_text(encoding="utf-8"),
            "new_string": "name: replaced\n",
            "content_hash": _hash(door, path),
        }
        return test_client.patch(f"{BASE}/{path}", json=body)
    return test_client.delete(f"{BASE}/{path}")


def test_new_shared_pattern_may_be_created_via_rest(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    fresh = "xray-patterns/__any__/fresh.yaml"

    response = _rest(env, client, "create", fresh, _writer())

    assert response.status_code == 201, response.text
    assert (env.fx.root / fresh).read_text(encoding="utf-8") == "name: created\n"


def test_granted_scoped_pattern_write_via_rest(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    created = _rest(env, client, "create", GRANTED_SCOPED, _writer())
    edited = _rest(env, client, "edit", GRANTED_SCOPED, _writer())

    assert created.status_code == 201, created.text
    assert edited.status_code == 200, edited.text
    target = env.fx.root / GRANTED_SCOPED
    assert target.read_text(encoding="utf-8") == "name: replaced\n"


def test_ungranted_scoped_pattern_write_via_rest_is_refused(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    response = _rest(env, client, "create", UNGRANTED_SCOPED, _writer())

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": REFUSAL}
    assert not (env.fx.root / UNGRANTED_SCOPED).exists()


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_shared_pattern_replace_requires_admin(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    before = (env.fx.root / SHARED).read_bytes()

    data = _mcp(env, tool, SHARED, _writer())

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (env.fx.root / SHARED).read_bytes() == before


@pytest.mark.parametrize("verb", ["edit", "delete"])
def test_shared_pattern_replace_requires_admin_via_rest(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
    verb: str,
) -> None:
    before = (env.fx.root / SHARED).read_bytes()

    response = _rest(env, client, verb, SHARED, _writer())

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": SHARED_REFUSAL}
    assert (env.fx.root / SHARED).read_bytes() == before


def test_shared_pattern_create_over_existing_is_refused(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    before = (env.fx.root / SHARED).read_bytes()

    data = _mcp(env, "create", SHARED, _writer())

    assert data["success"] is False
    assert (env.fx.root / SHARED).read_bytes() == before


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_shared_pattern_change_requires_admin_role_not_admins_group(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    before = (env.fx.root / SHARED).read_bytes()

    data = _mcp(env, tool, SHARED, _admins_group_power_user())

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (env.fx.root / SHARED).read_bytes() == before


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_full_catalog_writer_cannot_replace_shared_pattern(
    power: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    before = (power.fx.root / SHARED).read_bytes()

    data = _mcp(power, tool, SHARED, user(POWER_USER, role=UserRole.POWER_USER))

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (power.fx.root / SHARED).read_bytes() == before


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_shared_pattern_reached_through_a_link_requires_admin(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    link = env.fx.root / "xray-patterns" / ALPHA / "shared-link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(env.fx.root / "xray-patterns" / "__any__", target_is_directory=True)
    before = (env.fx.root / SHARED).read_bytes()

    data = _mcp(env, tool, f"xray-patterns/{ALPHA}/shared-link/p.yaml", _writer())

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (env.fx.root / SHARED).read_bytes() == before


def test_admin_may_replace_and_delete_shared_pattern(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    edited = _mcp(env, "edit", SHARED, _admin())
    assert edited["success"] is True, edited
    assert (env.fx.root / SHARED).read_text(encoding="utf-8") == "name: replaced\n"

    deleted = _mcp(env, "delete", SHARED, _admin())
    assert deleted["success"] is True, deleted
    assert not (env.fx.root / SHARED).exists()


def test_admin_may_replace_shared_pattern_via_rest(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    response = _rest(env, client, "edit", SHARED, _admin())

    assert response.status_code == 200, response.text
    assert (env.fx.root / SHARED).read_text(encoding="utf-8") == "name: replaced\n"


def test_new_shared_pattern_may_be_created_by_a_writer(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    data = _mcp(env, "create", "xray-patterns/__any__/fresh.yaml", _writer())

    assert data["success"] is True, data
    assert (env.fx.root / "xray-patterns" / "__any__" / "fresh.yaml").is_file()


@pytest.mark.parametrize("tool", ["create", "edit", "delete"])
def test_scoped_pattern_write_requires_a_grant(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    if tool != "create":
        _put(env, UNGRANTED_SCOPED)
    target = env.fx.root / UNGRANTED_SCOPED
    before = target.read_bytes() if target.exists() else None

    data = _mcp(env, tool, UNGRANTED_SCOPED, _writer())

    assert data == {"success": False, "error": REFUSAL}
    assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.parametrize("tool", ["create", "edit", "delete"])
def test_granted_scoped_pattern_write_succeeds(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    if tool != "create":
        _put(env, GRANTED_SCOPED)

    data = _mcp(env, tool, GRANTED_SCOPED, _writer())

    assert data["success"] is True, data
    target = env.fx.root / GRANTED_SCOPED
    expected: Optional[str] = {
        "create": "name: created\n",
        "edit": "name: replaced\n",
    }.get(tool)
    assert (target.read_text(encoding="utf-8") if target.exists() else None) == expected


@pytest.fixture
def no_writer_call(env: CatalogFileDoorEnv) -> Iterator[Any]:  # noqa: F811
    """The service called on the catalog without a pattern writer."""

    def call(path: str) -> Any:
        return file_crud_service.edit_file(
            repo_alias=CATALOG_ALIAS,
            file_path=path,
            old_string="name: p\n",
            new_string="name: replaced\n",
            content_hash=_hash(env, path),
            replace_all=False,
            username=ADMIN_USER,
            access_for=lambda _served: UNRESTRICTED,
        )

    yield call


def test_catalog_pattern_write_without_a_writer_is_refused(
    env: CatalogFileDoorEnv,  # noqa: F811
    no_writer_call: Any,
) -> None:
    before = (env.fx.root / SHARED).read_bytes()

    with pytest.raises(PermissionError):
        no_writer_call(SHARED)

    assert (env.fx.root / SHARED).read_bytes() == before


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_shared_pattern_change_is_refused_even_when_absent_at_authorization(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    absent = "xray-patterns/__any__/absent.yaml"
    params: Dict[str, Any] = {"repository_alias": CATALOG_ALIAS, "file_path": absent}
    if tool == "edit":
        params.update(old_string="a", new_string="b", content_hash="0" * 64)
        handler = files_module.handle_edit_file
    else:
        handler = files_module.handle_delete_file
    with env.serving():
        data = mcp_data(handler(params, _writer()))

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert not (env.fx.root / absent).exists()


def test_edit_through_a_shared_link_requires_admin(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    _put(env, GRANTED_SCOPED)
    link = env.fx.root / "xray-patterns" / "__any__" / "alias.yaml"
    link.symlink_to(env.fx.root / GRANTED_SCOPED)
    before = (env.fx.root / GRANTED_SCOPED).read_bytes()

    data = _mcp(env, "edit", "xray-patterns/__any__/alias.yaml", _writer())

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (env.fx.root / GRANTED_SCOPED).read_bytes() == before


def test_create_through_an_ungranted_link_is_refused(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    link = env.fx.root / UNGRANTED_SCOPED
    link.parent.mkdir(parents=True, exist_ok=True)
    landing = env.fx.root / "xray-patterns" / ALPHA / "landed.yaml"
    link.symlink_to(landing)

    data = _mcp(env, "create", UNGRANTED_SCOPED, _writer())

    assert data == {"success": False, "error": REFUSAL}
    assert not landing.exists()


def _link_shared_directory(door: CatalogFileDoorEnv) -> None:
    """``xray-patterns/__any__`` becomes a link to another catalog directory."""
    import shutil

    shared = door.fx.root / "xray-patterns" / "__any__"
    shutil.move(str(shared), str(door.fx.root / "elsewhere"))
    shared.symlink_to(door.fx.root / "elsewhere", target_is_directory=True)


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_shared_pattern_under_a_linked_shared_directory_requires_admin(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    _link_shared_directory(env)
    before = (env.fx.root / "elsewhere" / "p.yaml").read_bytes()

    data = _mcp(env, tool, SHARED, _admins_group_power_user())

    assert data == {"success": False, "error": SHARED_REFUSAL}
    assert (env.fx.root / "elsewhere" / "p.yaml").read_bytes() == before


def test_admin_may_edit_under_a_linked_shared_directory(
    env: CatalogFileDoorEnv,  # noqa: F811
) -> None:
    _link_shared_directory(env)

    data = _mcp(env, "edit", SHARED, _admin())

    assert data["success"] is True, data
    assert (env.fx.root / "elsewhere" / "p.yaml").read_text() == "name: replaced\n"


@pytest.mark.parametrize("tool", ["edit", "delete"])
def test_scoped_pattern_under_a_linked_scope_directory_requires_a_grant(
    env: CatalogFileDoorEnv,  # noqa: F811
    tool: str,
) -> None:
    granted = f"xray-patterns/{ALPHA}/p.yaml"
    _put(env, granted)
    (env.fx.root / "xray-patterns" / GAMMA).symlink_to(
        env.fx.root / "xray-patterns" / ALPHA, target_is_directory=True
    )
    before = (env.fx.root / granted).read_bytes()

    data = _mcp(env, tool, f"xray-patterns/{GAMMA}/p.yaml", _writer())

    assert data == {"success": False, "error": REFUSAL}
    assert (env.fx.root / granted).read_bytes() == before


def test_create_never_replaces_a_pattern_created_concurrently(
    env: CatalogFileDoorEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raced = "xray-patterns/__any__/raced.yaml"
    target = env.fx.root / raced
    real_fsync = crud_module.nfs_safe_fsync

    def fsync_then_concurrent_create(fd: int) -> None:
        real_fsync(fd)
        if not target.exists():
            target.write_text("name: admin\n", encoding="utf-8")

    monkeypatch.setattr(crud_module, "nfs_safe_fsync", fsync_then_concurrent_create)

    data = _mcp(env, "create", raced, _writer())

    assert data == {"success": False, "error": f"File already exists: {raced}"}
    assert target.read_text(encoding="utf-8") == "name: admin\n"
    assert not list(target.parent.glob(".tmp_*"))
