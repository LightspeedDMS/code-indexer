"""REST file write routes require the repository write permission.

The MCP file write tools require ``repository:write``; the REST routes that
create, edit and delete files require the same permission through the same
check. A caller whose role lacks it is refused before any repository is
resolved, for an ordinary activation and for the catalog alike; power users
and admins are unchanged. Real routes on a small FastAPI app, real
FileCRUDService, real files on disk. Neutral names only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
)
from tests.unit.server.mcp.catalog_file_door_env import (
    CATALOG_ALIAS,
    CatalogFileDoorEnv,
)
from tests.unit.server.routers.test_catalog_write_gate_rest import (  # noqa: F401
    client,
    env,
)

DENIED = {"detail": "Insufficient permissions: repository:write required"}
PLAIN = "plain"


@pytest.fixture(autouse=True)
def _server_state(
    env: CatalogFileDoorEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activated-repository access check reads the server app's state."""
    from types import SimpleNamespace

    from code_indexer.server import app as app_module
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )

    access = AccessFilteringService(
        env.fx.group_manager,
        memories_dir=env.fx.root / "memories",
        activated_repo_manager=env.activations,  # type: ignore[arg-type]
    )
    state = SimpleNamespace(**{**vars(env.state), "access_filtering_service": access})
    # ^ why setattr reads the old value first=>lazy app build; set the dict
    monkeypatch.setitem(vars(app_module), "app", SimpleNamespace(state=state))


def _caller(role: UserRole, name: str = RESTRICTED_USER) -> User:
    return User(
        username=name, password_hash="hash", role=role, created_at=datetime.now()
    )


def _plain(door: CatalogFileDoorEnv, tmp_path: Path) -> Path:
    repo = tmp_path / "activated" / PLAIN
    repo.mkdir(parents=True)
    (repo / "existing.txt").write_text("a\n")
    door.activations.add(PLAIN, ALPHA, repo)
    return repo


def _send(test_client: TestClient, verb: str, alias: str) -> Any:
    base = f"/api/v1/repos/{alias}/files"
    if verb == "create":
        return test_client.post(base, json={"file_path": "new.txt", "content": "x\n"})
    if verb == "edit":
        body = {"old_string": "a", "new_string": "b", "content_hash": "0" * 64}
        return test_client.patch(f"{base}/existing.txt", json=body)
    return test_client.delete(f"{base}/existing.txt")


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.mark.parametrize("verb", ["create", "edit", "delete"])
@pytest.mark.parametrize("target", ["ordinary", "catalog"])
def test_lower_role_gets_the_permission_refusal(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
    tmp_path: Path,
    verb: str,
    target: str,
) -> None:
    repo = _plain(env, tmp_path)
    root = repo if target == "ordinary" else env.fx.root
    alias = PLAIN if target == "ordinary" else CATALOG_ALIAS
    before = _snapshot(root)
    client.app.dependency_overrides[get_current_user] = lambda: _caller(  # type: ignore[attr-defined]
        UserRole.NORMAL_USER
    )

    response = _send(client, verb, alias)

    assert response.status_code == 403, response.text
    assert response.json() == DENIED
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "caller",
    [_caller(UserRole.POWER_USER), _caller(UserRole.ADMIN, ADMIN_USER)],
    ids=["power_user", "admin"],
)
def test_power_user_and_admin_keep_writing(
    env: CatalogFileDoorEnv,  # noqa: F811
    client: TestClient,  # noqa: F811
    tmp_path: Path,
    caller: User,
) -> None:
    repo = _plain(env, tmp_path)
    client.app.dependency_overrides[get_current_user] = lambda: caller  # type: ignore[attr-defined]

    response = _send(client, "create", PLAIN)

    assert response.status_code == 201, response.text
    assert (repo / "new.txt").read_text() == "x\n"
