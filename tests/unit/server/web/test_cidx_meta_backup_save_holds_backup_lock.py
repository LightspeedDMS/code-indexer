"""The Web UI save of the cidx-meta backup settings bootstraps the backup
(git init, stage-all, commit, push) only while holding the catalog write
lock as the exclusive backup owner. When another owner holds the catalog
lock the settings are still saved, nothing is bootstrapped, and the
refresh is owed to the debouncer (the refresh's backup phase bootstraps).

Real components: the real route, a real file WriteLockManager over a
temporary golden-repos directory, the real backup bootstrap against a real
bare origin. Only the session, CSRF, elevation and page rendering are
stubbed (the route's own authentication surface, not under test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.write_lock_manager import WriteLockManager

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"
CATALOG = "cidx-meta"


class _ConfigService:
    def __init__(self, server_dir: Path) -> None:
        self.config_manager = SimpleNamespace(server_dir=server_dir)
        self.saved: List[Any] = []

    def update_settings_audited(self, changes: Any, actor: str) -> None:
        self.saved.append((list(changes), actor))


class _Debouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


def _client(
    monkeypatch: pytest.MonkeyPatch,
    config_service: _ConfigService,
    locks: Optional[WriteLockManager],
) -> TestClient:
    from code_indexer.server.web import routes

    app = FastAPI()
    app.include_router(routes.web_router, prefix="/admin")
    for route in routes.web_router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            check = getattr(dep, "dependency", None)
            if check and getattr(check, "__qualname__", "") == _ELEVATION_QUALNAME:
                app.dependency_overrides[check] = lambda: None
    if locks is not None:
        app.state.global_lifecycle_manager = SimpleNamespace(
            refresh_scheduler=SimpleNamespace(write_lock_manager=locks)
        )
    monkeypatch.setattr(
        routes,
        "_require_admin_session",
        lambda request: SimpleNamespace(username="example-admin"),
    )
    monkeypatch.setattr(routes, "validate_login_csrf_token", lambda r, t: True)

    def _page(
        request: Any,
        session: Any,
        success_message: Optional[str] = None,
        error_message: Optional[str] = None,
        validation_errors: Optional[Dict[str, str]] = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return HTMLResponse(
            content=success_message or error_message or "", status_code=status_code
        )

    monkeypatch.setattr(routes, "_create_config_page_response", _page)
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: config_service,
    )
    return TestClient(app)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    server_dir = tmp_path / "server"
    golden = server_dir / "data" / "golden-repos"
    meta = golden / CATALOG
    meta.mkdir(parents=True)
    (meta / "example-repo.md").write_text("# example-repo\n")
    origin = tmp_path / "cidx-meta-backup.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    debouncer = _Debouncer()
    monkeypatch.setattr(meta_description_hook, "_debouncer", debouncer)
    return {
        "meta": meta,
        "origin": origin,
        "locks": WriteLockManager(golden),
        "config": _ConfigService(server_dir),
        "debouncer": debouncer,
    }


def _save(client: TestClient, origin: Path) -> Any:
    return client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": origin.as_uri(), "csrf_token": "x"},
    )


def _origin_commits(origin: Path) -> str:
    return subprocess.run(
        ["git", "log", "--all", "--oneline"], cwd=origin, capture_output=True, text=True
    ).stdout.strip()


def test_save_never_bootstraps_while_another_owner_holds_the_catalog(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any]
) -> None:
    locks: WriteLockManager = world["locks"]
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        response = _save(_client(monkeypatch, world["config"], locks), world["origin"])
        assert response.status_code == 200, response.text
        assert "busy" in response.text
        assert world["config"].saved, "the settings were not saved"
        assert not (world["meta"] / ".git").exists(), "bootstrapped under a held lock"
        assert _origin_commits(world["origin"]) == ""
        assert world["debouncer"].signals == 1, "the refresh was not owed"
        info = locks.get_lock_info(CATALOG)
        assert info and info["owner"] == "mcp_write_mode"
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")


def test_save_bootstraps_under_the_exclusive_backup_owner(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any]
) -> None:
    from code_indexer.global_repos.catalog_lock_holder import (
        CATALOG_BACKUP_LOCK_OWNER,
    )
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    locks: WriteLockManager = world["locks"]
    holders: List[Any] = []
    real_bootstrap = CidxMetaBackupBootstrap.bootstrap

    def _observed(self: Any, cidx_meta_path: str, remote_url: str) -> str:
        info = locks.get_lock_info(CATALOG)
        holders.append(info["owner"] if info else None)
        return real_bootstrap(self, cidx_meta_path, remote_url)

    monkeypatch.setattr(CidxMetaBackupBootstrap, "bootstrap", _observed)
    response = _save(_client(monkeypatch, world["config"], locks), world["origin"])

    assert response.status_code == 200, response.text
    assert holders == [CATALOG_BACKUP_LOCK_OWNER]
    assert (world["meta"] / ".git").exists()
    assert _origin_commits(world["origin"]) != ""
    assert locks.get_lock_info(CATALOG) is None
    assert world["debouncer"].signals == 0


def test_save_with_a_remote_needs_the_catalog_lock(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any]
) -> None:
    response = _save(_client(monkeypatch, world["config"], None), world["origin"])

    assert response.status_code == 500, response.text
    assert "nothing was saved" in response.text
    assert world["config"].saved == [], "settings saved without a way to bootstrap"
    assert not (world["meta"] / ".git").exists()
