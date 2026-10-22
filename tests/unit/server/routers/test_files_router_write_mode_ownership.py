"""REST catalog writes under write mode require the write-mode lock.

A write-mode marker alone never authorizes a write: the REST create, edit and
delete routes write only while the marker's exact token still owns the
write-mode lock (its lease is renewed by that token first). Once another
holder took the lock, or its lease lapsed, the route answers 403 telling the
caller to re-enter write mode, and nothing is written. Real REST router, real
FileCRUDService, real lock stores (SQLite, PostgreSQL via TEST_POSTGRES_DSN).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.handlers._write_mode_marker import (
    WRITE_MODE_OWNER,
    renew_write_mode_lease,
    write_marker,
)
from code_indexer.server.services import file_crud_service as crud_module
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from code_indexer.server.services.file_crud_service import file_crud_service
from tests.unit.server.mcp.catalog_file_door_env import (
    CATALOG_ALIAS,
    CatalogFileDoorEnv,
    user,
)

_ALIAS = "cidx-meta"
_TOKEN = "write-mode-token"
_VISIBLE = "xray-patterns/__any__/n.yaml"
BASE = f"/api/v1/repos/{CATALOG_ALIAS}/files"


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    if request.param == "sqlite":
        yield lambda: SqliteAliasLockStore(tmp_path / "alias_locks")
        return
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )
    from tests.shared.alias_lock_pg import postgres_lock_store_dsn

    with postgres_lock_store_dsn(_ALIAS) as dsn:
        yield lambda: PostgresAliasLockStore(dsn)


def _coordinator(golden: Path, store: Any) -> AliasLockCoordinator:
    return AliasLockCoordinator(
        golden_repos_dir=golden,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    return CatalogFileDoorEnv(tmp_path)


@pytest.fixture
def client(
    env: CatalogFileDoorEnv, make_store: Callable[[], Any], monkeypatch
) -> Iterator[TestClient]:
    from code_indexer.server.routers.files import router

    locks = _coordinator(env.golden_root, make_store())
    assert locks.acquire(_ALIAS, owner_name=WRITE_MODE_OWNER, owner_token=_TOKEN)
    write_marker(env.golden_root, _ALIAS, str(env.fx.root), _TOKEN)
    worker = SimpleNamespace(
        write_lock_manager=_coordinator(env.golden_root, make_store())
    )
    monkeypatch.setattr(
        file_crud_service,
        "_write_mode_lease_renewer",
        lambda golden, alias: renew_write_mode_lease(worker, golden, alias),
        raising=False,
    )
    monkeypatch.setattr(
        file_crud_service,
        "_global_write_exceptions",
        {CATALOG_ALIAS: env.fx.root},
    )
    monkeypatch.setattr(file_crud_service, "_golden_repos_dir", env.golden_root)
    app = FastAPI()
    for name, value in vars(env.state).items():
        setattr(app.state, name, value)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user(role=UserRole.POWER_USER)
    with (
        patch.object(
            crud_module, "_get_activated_repo_manager", lambda: env.activations
        ),
        TestClient(app, raise_server_exceptions=False) as test_client,
    ):
        test_client.locks = locks  # type: ignore[attr-defined]
        test_client.store = make_store()  # type: ignore[attr-defined]
        yield test_client


def _send(client: TestClient, verb: str, path: str) -> Any:
    if verb == "create":
        return client.post(BASE, json={"file_path": path, "content": "probe\n"})
    if verb == "edit":
        body = {"old_string": "a", "new_string": "b", "content_hash": "0" * 64}
        return client.patch(f"{BASE}/{path}", json=body)
    return client.delete(f"{BASE}/{path}")


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _lapse(store: Any) -> None:
    if isinstance(store, SqliteAliasLockStore):
        path = str(store._lock_dir / f"{_ALIAS}.db")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE golden_repo_alias_locks SET last_renewed_at = "
                "strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 hours')"
            )
        return
    import psycopg

    with closing(psycopg.connect(store._dsn)) as pg, pg:
        pg.execute(
            "UPDATE golden_repo_alias_locks SET last_renewed_at = "
            "now() - interval '2 hours' WHERE lock_key = %s",
            (_ALIAS,),
        )


class TestRestWriteModeOwnership:
    def test_write_proceeds_while_the_marker_token_owns_the_lock(
        self, client: TestClient, env: CatalogFileDoorEnv
    ) -> None:
        response = _send(client, "create", _VISIBLE)

        assert response.status_code == 201, response.text
        assert (env.fx.root / _VISIBLE).is_file()

    @pytest.mark.parametrize("verb", ["create", "edit", "delete"])
    def test_write_after_another_holder_took_the_lock_is_refused(
        self, client: TestClient, env: CatalogFileDoorEnv, verb: str
    ) -> None:
        locks = client.locks  # type: ignore[attr-defined]
        assert locks.release(_ALIAS, owner_name=WRITE_MODE_OWNER, owner_token=_TOKEN)
        assert locks.acquire(_ALIAS, owner_name="refresh_scheduler")
        before = _snapshot(env.fx.root)

        response = _send(client, verb, _VISIBLE)

        assert response.status_code == 403, response.text
        assert "enter_write_mode" in response.json()["detail"]
        assert _snapshot(env.fx.root) == before

    def test_write_after_the_lease_lapsed_is_refused(
        self, client: TestClient, env: CatalogFileDoorEnv
    ) -> None:
        _lapse(client.store)  # type: ignore[attr-defined]

        response = _send(client, "create", _VISIBLE)

        assert response.status_code == 403, response.text
        assert "enter_write_mode" in response.json()["detail"]
        assert not (env.fx.root / _VISIBLE).exists()
