"""Async doors run their complete synchronous operation off the event loop.

Each test drives a real ``async def`` door and records, inside the real
component's blocking call, whether that call ran on a thread that owns a
running event loop.  The recording subclasses change nothing else about the
real components.  The tests also assert that no "durable audit emitted on the
event loop" ERROR was logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
    make_user_manager,
)
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager

_ADMIN = "example-admin"
_PASSWORD = "SecureP@ssw0rd!XyZ789"
_CSRF = "example-csrf-token"
_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _CallLog:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, bool]] = []

    def note(self, name: str) -> None:
        self.calls.append((name, _on_event_loop()))

    def on_loop(self) -> List[str]:
        return [name for name, on_loop in self.calls if on_loop]


class _RecordingGroupManager(GroupAccessManager):
    """The real GroupAccessManager, noting which thread runs each call."""

    log = _CallLog()

    def get_group(self, group_id):  # type: ignore[no-untyped-def]
        self.log.note("get_group")
        return super().get_group(group_id)

    def grant_repo_access(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.log.note("grant_repo_access")
        return super().grant_repo_access(*args, **kwargs)

    def revoke_repo_access(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.log.note("revoke_repo_access")
        return super().revoke_repo_access(*args, **kwargs)

    def get_user_group(self, username):  # type: ignore[no-untyped-def]
        self.log.note("get_user_group")
        return super().get_user_group(username)

    def remove_user_from_group(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.log.note("remove_user_from_group")
        return super().remove_user_from_group(*args, **kwargs)


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


@pytest.fixture()
def web(tmp_path: Path, store, monkeypatch, caplog):
    from code_indexer.server.auth import dependencies
    from code_indexer.server.auth.oidc import routes as oidc_routes
    from code_indexer.server.web.routes import web_router

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    _RecordingGroupManager.log = _CallLog()
    groups = _RecordingGroupManager(tmp_path / "groups.db")
    users = make_user_manager(tmp_path)
    users.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    monkeypatch.setattr(dependencies, "user_manager", users)
    monkeypatch.setattr(oidc_routes, "oidc_manager", None)

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(web_router, prefix="/admin")
    for route in web_router.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependencies or []:
                fn = getattr(dep, "dependency", None)
                if fn and getattr(fn, "__qualname__", "") == _ELEVATION_QUALNAME:
                    app.dependency_overrides[fn] = lambda: None
    session = MagicMock()
    session.username = _ADMIN
    session.role = "admin"
    patches = {
        "_require_admin_session": session,
        "_get_group_manager": groups,
        "get_csrf_token_from_cookie": _CSRF,
        "validate_login_csrf_token": True,
    }
    with contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(
                patch(f"code_indexer.server.web.routes.{name}", return_value=value)
            )
        yield TestClient(app), groups, users


def _repo_access(client: TestClient, action: str, group_id: int):
    return client.post(
        f"/admin/groups/repo-access/{action}",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": _CSRF,
            "Content-Type": "application/json",
        },
        json={"repo_name": "example-repo", "group_id": group_id},
    )


def test_web_repo_access_grant_and_revoke_run_off_the_loop(web, caplog) -> None:
    client, groups, _users = web
    group = groups.create_group(name="example-group", description="")
    assert _repo_access(client, "grant", group.id).status_code == 200
    assert _repo_access(client, "revoke", group.id).status_code == 200
    names = [name for name, _ in _RecordingGroupManager.log.calls]
    assert {"get_group", "grant_repo_access", "revoke_repo_access"} <= set(names)
    assert _RecordingGroupManager.log.on_loop() == []
    assert capture_errors(caplog) == []


def test_web_user_delete_runs_its_group_cleanup_off_the_loop(
    web, store, caplog
) -> None:
    client, groups, users = web
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    group = groups.create_group(name="example-group", description="")
    groups.assign_user_to_group("example-user", group.id, _ADMIN)
    resp = client.post(
        "/admin/users/example-user/delete", data={}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    names = [name for name, _ in _RecordingGroupManager.log.calls]
    assert {"get_user_group", "remove_user_from_group"} <= set(names)
    assert _RecordingGroupManager.log.on_loop() == []
    assert capture_errors(caplog) == []
    assert [r.outcome for r in store.rows("user_deleted")] == ["success"]


def test_web_git_credential_add_routes_build_their_manager_off_the_loop(
    tmp_path: Path, store, monkeypatch, caplog
) -> None:
    from code_indexer.server.services import config_service as config_module
    from code_indexer.server.services import git_credential_manager as gcm
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.web import routes as web_routes

    log = _CallLog()
    server_dir = tmp_path / "server"
    (server_dir / "data").mkdir(parents=True)
    DatabaseSchema(str(server_dir / "data" / "cidx_server.db")).initialize_database()
    config_svc = config_module.ConfigService(server_dir_path=str(server_dir))
    config_svc.load_config()
    monkeypatch.setattr(config_module, "_config_service", config_svc)
    real_load_config = config_svc.config_manager.load_config

    def _load_config():  # type: ignore[no-untyped-def]
        log.note("load_config")
        return real_load_config()

    monkeypatch.setattr(config_svc.config_manager, "load_config", _load_config)
    real_create = gcm.create_git_credential_manager

    def _create(*args, **kwargs):  # type: ignore[no-untyped-def]
        log.note("create_git_credential_manager")
        return real_create(*args, **kwargs)

    class _Forge:
        async def validate_and_discover(self, token: str, host: str):
            return {"git_user_name": "Example", "forge_username": "example"}

    monkeypatch.setattr(gcm, "create_git_credential_manager", _create)
    monkeypatch.setattr(gcm, "get_forge_client", lambda _t: _Forge())

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(web_routes.web_router, prefix="/admin")
    app.include_router(web_routes.user_router, prefix="/user")
    for router in (web_routes.web_router, web_routes.user_router):
        for route in router.routes:
            if isinstance(route, APIRoute):
                for dep in route.dependencies or []:
                    fn = getattr(dep, "dependency", None)
                    if fn is not None and getattr(fn, "__qualname__", "") in (
                        _ELEVATION_QUALNAME,
                        "_git_credential_self_elevation",
                    ):
                        app.dependency_overrides[fn] = lambda: None
    session = MagicMock()
    session.username = _ADMIN
    session.role = "admin"
    monkeypatch.setattr(web_routes, "_require_admin_session", lambda _r: session)
    monkeypatch.setattr(
        web_routes, "_require_authenticated_session", lambda _r: session
    )
    client = TestClient(app)
    body = {"forge_type": "github", "forge_host": "github.com", "token": "tok-0000"}
    for prefix in ("/admin", "/user"):
        resp = client.post(f"{prefix}/git-credentials", json=body)
        assert resp.status_code == 200, resp.text
    names = [name for name, _ in log.calls]
    assert names.count("load_config") >= 2
    assert names.count("create_git_credential_manager") == 2
    assert log.on_loop() == []
    assert capture_errors(caplog) == []


async def test_git_credential_configure_writes_off_the_loop(
    store, tmp_path: Path, monkeypatch, caplog
) -> None:
    from code_indexer.server.services import git_credential_manager as gcm
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        GitCredentialsSqliteBackend,
    )

    log = _CallLog()

    class _RecordingBackend(GitCredentialsSqliteBackend):
        def upsert_credential(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            log.note("upsert_credential")
            return super().upsert_credential(*args, **kwargs)

    class _Forge:
        async def validate_and_discover(self, token: str, host: str):
            return {"git_user_name": "Example", "forge_username": "example"}

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    monkeypatch.setattr(gcm, "get_forge_client", lambda _t: _Forge())
    db_path = str(tmp_path / "creds.db")
    DatabaseSchema(db_path=db_path).initialize_database()
    manager = gcm.GitCredentialManager(
        db_path=db_path, backend=_RecordingBackend(db_path)
    )
    await manager.configure_credential_audited(
        "example-user", "github", "github.com", "tok-0000", actor="example-user"
    )
    assert log.calls == [("upsert_credential", False)]
    assert capture_errors(caplog) == []


async def test_sso_account_lookups_run_off_the_loop(
    store, tmp_path: Path, caplog
) -> None:
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    log = _CallLog()
    users = make_user_manager(tmp_path)
    users.create_user("example-sso-user", _PASSWORD, UserRole.NORMAL_USER)
    users.update_user("example-sso-user", new_email="person@example.com")
    real_get_user = users.get_user
    real_get_user_by_email = users.get_user_by_email

    def _get_user(username):  # type: ignore[no-untyped-def]
        log.note("get_user")
        return real_get_user(username)

    def _get_user_by_email(email):  # type: ignore[no-untyped-def]
        log.note("get_user_by_email")
        return real_get_user_by_email(email)

    users.get_user = _get_user  # type: ignore[method-assign]
    users.get_user_by_email = _get_user_by_email  # type: ignore[method-assign]

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )
    manager = OIDCManager(config, users, None)
    manager.db_path = str(tmp_path / "oidc.db")
    await manager._init_db()
    # Auto-link by email (existing account), then JIT for a new account.
    linked = await manager.match_or_create_user(
        OIDCUserInfo(
            subject="example-subject-1",
            email="person@example.com",
            email_verified=True,
            username="example-sso-user",
        )
    )
    created = await manager.match_or_create_user(
        OIDCUserInfo(
            subject="example-subject-2",
            email="other@example.com",
            email_verified=True,
            username="example-new-user",
        )
    )
    assert linked is not None and created is not None
    assert "get_user_by_email" in [name for name, _ in log.calls]
    assert log.on_loop() == []
    assert capture_errors(caplog) == []
