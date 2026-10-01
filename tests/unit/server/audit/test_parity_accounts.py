"""Front-door parity for account, credential and permission audit rows.

Every capability writes its one row on every door where it exists (REST,
MCP, Web), with the authenticated caller as the actor (never the target),
the door it entered through as the source, and no secret in any column.

Front doors: the real REST routes and Web routers (TestClient, with the
audit request context middleware) and the real MCP JSON-RPC tools/call
dispatcher, over real auth components on isolated files
(``self_service_elevation_harness``), a real SSH key manager in temporary
directories, real group and credential stores and a real audit store.
Elevation enforcement is off (both read points), the Web CSRF check is
replaced, and the external forge API client is a fake.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditRow,
    AuditStore,
    bound_audit_store,
    capture_errors,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
    bind_audit_request_context,
    build_request_context,
    reset_audit_request_context,
)
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
)

_ADMIN = "example-admin"
_OTHER = "example-other"
_PASSWORD = "SecureP@ssw0rd!XyZ789"
_NEW_PASSWORD = "Another$ecureP4ss!Qw"
_FORGE_HOST = "forge.example.com"
_TOKEN = "ghp_exampleTokenValue0000000000000000"


class _FakeForgeClient:
    """Stands in for the external forge API (network boundary)."""

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
        return {"git_user_name": "Example", "forge_username": "example"}


class _Env:
    def __init__(
        self,
        stack: SelfServiceStack,
        store: AuditStore,
        client: TestClient,
        admin: User,
        other: User,
        groups: Any,
    ) -> None:
        self.stack = stack
        self.store = store
        self.client = client
        self.admin = admin
        self.other = other
        self.groups = groups
        self.bearer, _jti = stack.bearer(admin)
        self.cookie = stack.session_cookie(admin)

    # ---------------------------------------------------------------- doors

    def rest(self, method: str, path: str, **kwargs: Any):
        headers = {"Authorization": f"Bearer {self.bearer}"}
        return self.client.request(method, path, headers=headers, **kwargs)

    def web(self, method: str, path: str, **kwargs: Any):
        self.client.cookies.set(SESSION_COOKIE_NAME, self.cookie)
        try:
            return self.client.request(method, path, follow_redirects=False, **kwargs)
        finally:
            self.client.cookies.clear()

    def mcp(
        self, tool: str, arguments: Dict[str, Any], session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """The MCP tools/call dispatcher, attributed as the /mcp door."""
        import asyncio

        from code_indexer.server.mcp.protocol import process_jsonrpc_request

        token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
        try:
            response = asyncio.run(
                process_jsonrpc_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    self.admin,
                    session_id=session_id,
                )
            )
        finally:
            reset_audit_request_context(token)
        assert "result" in response, response
        payload: Dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
        return payload

    # ---------------------------------------------------------------- rows

    def rows(self, action_type: str) -> List[AuditRow]:
        return [r for r in self.store.rows(action_type) if r.action_type == action_type]

    def only_row(self, action_type: str) -> AuditRow:
        rows = self.rows(action_type)
        assert len(rows) == 1, rows
        return rows[0]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[_Env]:
    from code_indexer.server.auth import dependencies
    from code_indexer.server.auth.audit_logger import password_audit_logger
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
    from code_indexer.server.mcp.handlers import ssh_keys as mcp_ssh_keys
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.routers import groups as groups_router
    from code_indexer.server.routers import ssh_keys as rest_ssh_keys
    from code_indexer.server.routers.inline_admin_users import (
        register_admin_user_routes,
    )
    from code_indexer.server.routers.inline_auth import register_auth_routes
    from code_indexer.server.routers.inline_mcp_creds import (
        register_mcp_credential_routes,
    )
    from code_indexer.server.services import config_service as config_service_module
    from code_indexer.server.services import git_credential_manager as gcm
    from code_indexer.server.services.group_access_manager import GroupAccessManager
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.web import routes as web_routes

    stack = build_stack(tmp_path, monkeypatch)
    admin = stack.user_manager.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    other = stack.user_manager.create_user(_OTHER, _PASSWORD, UserRole.NORMAL_USER)

    # Credential and group stores the doors resolve, in this test's own
    # server directory (never an inherited process-wide one).
    server_dir = tmp_path / "server"
    (server_dir / "data").mkdir(parents=True)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    config_svc = config_service_module.ConfigService(server_dir_path=str(server_dir))
    config_svc.load_config()
    monkeypatch.setattr(config_service_module, "_config_service", config_svc)
    DatabaseSchema(str(server_dir / "data" / "cidx_server.db")).initialize_database()
    monkeypatch.setattr(gcm, "get_forge_client", lambda _t: _FakeForgeClient())
    mcp_manager = MCPCredentialManager(user_manager=stack.user_manager)
    monkeypatch.setattr(dependencies, "mcp_credential_manager", mcp_manager)
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    ssh = SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "meta" / "ssh_keys",
        config_path=ssh_dir / "config",
    )
    monkeypatch.setattr(rest_ssh_keys, "_ssh_key_manager", ssh)
    monkeypatch.setattr(mcp_ssh_keys, "_ssh_key_manager", ssh)
    monkeypatch.setattr(web_routes, "_get_ssh_key_manager", lambda: ssh)
    groups = GroupAccessManager(tmp_path / "groups.db")
    monkeypatch.setattr(groups_router, "_group_manager", groups)
    monkeypatch.setattr(web_routes, "_get_group_manager", lambda: groups)
    monkeypatch.setattr(app_module.app.state, "group_manager", groups, raising=False)
    monkeypatch.setattr(web_routes, "validate_login_csrf_token", lambda _r, _t: True)

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    managers: Dict[str, Any] = {
        "jwt_manager": stack.jwt_manager,
        "user_manager": stack.user_manager,
    }
    register_admin_user_routes(
        app, refresh_token_manager=None, db_path_str=str(tmp_path / "x.db"), **managers
    )
    register_auth_routes(app, refresh_token_manager=None, **managers)
    register_mcp_credential_routes(
        app,
        mcp_credential_manager=mcp_manager,
        mcp_registration_service=None,
        **managers,
    )
    app.include_router(rest_ssh_keys.router)
    app.include_router(groups_router.router)
    app.include_router(web_routes.web_router, prefix="/admin")
    app.include_router(web_routes.user_router, prefix="/user")

    for store in bound_audit_store(tmp_path / "audit.db"):
        monkeypatch.setattr(password_audit_logger, "_audit_service", store.service)
        with enforcement(False):
            client = TestClient(app, raise_server_exceptions=False)
            yield _Env(stack, store, client, admin, other, groups)


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    """No door may emit on the event loop, drop a row or build a bad event."""
    import logging

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


def _assert_attributed(row: AuditRow, *, actor: str, source: str) -> None:
    assert row.actor == actor
    assert row.source == source
    assert row.outcome == "success"


# =========================================================================
# Users
# =========================================================================


class TestUsers:
    def test_rest_user_lifecycle(self, env) -> None:
        body = {
            "username": "example-rest",
            "password": _PASSWORD,
            "role": "normal_user",
        }
        assert env.rest("POST", "/api/admin/users", json=body).status_code == 201
        assert (
            env.rest(
                "PUT", "/api/admin/users/example-rest", json={"role": "power_user"}
            ).status_code
            == 200
        )
        assert (
            env.rest(
                "PUT",
                "/api/admin/users/example-rest/change-password",
                json={"new_password": _NEW_PASSWORD},
            ).status_code
            == 200
        )
        assert env.rest("DELETE", "/api/admin/users/example-rest").status_code == 200
        for action in (
            "user_created",
            "user_role_changed",
            "user_password_reset_by_admin",
            "user_deleted",
        ):
            row = env.only_row(action)
            _assert_attributed(row, actor=_ADMIN, source="rest")
            assert row.target_id == "example-rest"
        assert _NEW_PASSWORD not in env.store.all_raw_text()

    def test_rest_self_password_change_adds_no_admin_reset_row(self, env) -> None:
        env.rest(
            "PUT",
            "/api/users/change-password",
            json={"old_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        )
        assert env.rows("user_password_reset_by_admin") == []

    def test_mcp_create_user(self, env) -> None:
        result = env.mcp(
            "create_user",
            {"username": "example-mcp", "password": _PASSWORD, "role": "admin"},
        )
        assert result["success"] is True, result
        row = env.only_row("user_created")
        _assert_attributed(row, actor=_ADMIN, source="mcp")
        assert row.details == {"role": "admin", "provisioning": "admin"}

    def test_web_user_lifecycle(self, env) -> None:
        env.web(
            "POST",
            "/admin/users/create",
            data={
                "new_username": "example-web",
                "new_password": _PASSWORD,
                "confirm_password": _PASSWORD,
                "role": "normal_user",
            },
        )
        env.web("POST", "/admin/users/example-web/role", data={"role": "power_user"})
        env.web(
            "POST",
            "/admin/users/example-web/password",
            data={"new_password": _NEW_PASSWORD, "confirm_password": _NEW_PASSWORD},
        )
        env.web(
            "POST",
            "/admin/users/example-web/email",
            data={"new_email": "person@example.com"},
        )
        env.web("POST", "/admin/users/example-web/delete", data={})
        for action in (
            "user_created",
            "user_role_changed",
            "user_password_reset_by_admin",
            "user_email_changed",
            "user_deleted",
        ):
            row = env.only_row(action)
            _assert_attributed(row, actor=_ADMIN, source="web")
            assert row.target_id == "example-web"
        text = env.store.all_raw_text()
        assert _NEW_PASSWORD not in text and "person@example.com" not in text


class TestUnknownTargets:
    """An attempted change to a missing target writes one failure row per door.

    The REST responses are unchanged (404 with the same body); the row is
    the same placeholder failure row the MCP / Web twins write.
    """

    def _failure(self, env, action: str, placeholder: str) -> None:
        (row,) = env.rows(action)
        assert (row.actor, row.outcome, row.target_id, row.details) == (
            _ADMIN,
            "failure",
            placeholder,
            {},
        )

    def test_rest_role_change_of_unknown_user(self, env) -> None:
        resp = env.rest(
            "PUT", "/api/admin/users/example-missing", json={"role": "admin"}
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "User not found: example-missing"}
        self._failure(env, "user_role_changed", "(unknown)")

    @staticmethod
    def _assert_error_redirect(resp) -> None:
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "error=user_not_found" in location
        assert "success=" not in location

    def test_web_role_change_of_unknown_user_writes_the_same_row(self, env) -> None:
        resp = env.web(
            "POST", "/admin/users/example-missing/role", data={"role": "admin"}
        )
        self._assert_error_redirect(resp)
        self._failure(env, "user_role_changed", "(unknown)")

    def test_web_password_reset_of_unknown_user_reports_an_error(self, env) -> None:
        resp = env.web(
            "POST",
            "/admin/users/example-missing/password",
            data={"new_password": _NEW_PASSWORD, "confirm_password": _NEW_PASSWORD},
        )
        self._assert_error_redirect(resp)
        self._failure(env, "user_password_reset_by_admin", "(unknown)")

    def test_web_email_change_of_unknown_user_reports_an_error(self, env) -> None:
        resp = env.web(
            "POST",
            "/admin/users/example-missing/email",
            data={"new_email": "person@example.com"},
        )
        self._assert_error_redirect(resp)
        self._failure(env, "user_email_changed", "(unknown)")

    def test_rest_delete_of_unknown_user(self, env) -> None:
        resp = env.rest("DELETE", "/api/admin/users/example-missing")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "User not found: example-missing"}
        self._failure(env, "user_deleted", "(unknown)")

    def test_web_delete_of_unknown_user_writes_the_same_row(
        self, env, monkeypatch
    ) -> None:
        cleanup_calls: List[str] = []
        for name in ("get_user_group", "remove_user_from_group"):
            monkeypatch.setattr(
                env.groups,
                name,
                lambda *a, _n=name, **k: cleanup_calls.append(_n),
            )
        resp = env.web("POST", "/admin/users/example-missing/delete", data={})
        self._assert_error_redirect(resp)
        assert cleanup_calls == []
        self._failure(env, "user_deleted", "(unknown)")

    def test_rest_credential_mint_for_unknown_user(self, env) -> None:
        resp = env.rest(
            "POST", "/api/admin/users/example-missing/mcp-credentials", json={}
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "User not found"}
        self._failure(env, "mcp_credential_created", "unresolved")

    def test_mcp_credential_mint_for_unknown_user_writes_the_same_row(
        self, env
    ) -> None:
        env.mcp(
            "manage_mcp_credential",
            {"action": "create", "target_user": "example-missing"},
        )
        self._failure(env, "mcp_credential_created", "unresolved")

    def test_rest_credential_revoke_for_unknown_user(self, env) -> None:
        resp = env.rest(
            "DELETE",
            "/api/admin/users/example-missing/mcp-credentials/"
            "4c7e1d2a-0000-4000-8000-000000000001",
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "User not found"}
        self._failure(env, "mcp_credential_revoked", "unresolved")

    def test_rest_revoke_of_unknown_credential_keeps_its_body(self, env) -> None:
        resp = env.rest(
            "DELETE",
            f"/api/admin/users/{_OTHER}/mcp-credentials/"
            "4c7e1d2a-0000-4000-8000-000000000002",
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Credential not found"}
        self._failure(env, "mcp_credential_revoked", "unresolved")

    def test_mcp_credential_revoke_for_unknown_user_writes_the_same_row(
        self, env
    ) -> None:
        env.mcp(
            "manage_mcp_credential",
            {
                "action": "delete",
                "target_user": "example-missing",
                "credential_id": "4c7e1d2a-0000-4000-8000-000000000001",
            },
        )
        self._failure(env, "mcp_credential_revoked", "unresolved")


# =========================================================================
# API keys
# =========================================================================


class TestApiKeys:
    def test_rest_create_and_delete(self, env) -> None:
        created = env.rest("POST", "/api/keys", json={"name": "free text name"})
        assert created.status_code == 201, created.text
        key_id = created.json()["key_id"]
        assert env.rest("DELETE", f"/api/keys/{key_id}").status_code == 200
        for action in ("api_key_created", "api_key_deleted"):
            row = env.only_row(action)
            _assert_attributed(row, actor=_ADMIN, source="rest")
            assert row.target_id == key_id
        text = env.store.all_raw_text()
        assert created.json()["api_key"] not in text and "free text name" not in text

    def test_mcp_create_and_delete(self, env) -> None:
        created = env.mcp("create_api_key", {"description": "free text name"})
        assert created["success"] is True, created
        deleted = env.mcp("delete_api_key", {"key_id": created["key_id"]})
        assert deleted["success"] is True, deleted
        for action in ("api_key_created", "api_key_deleted"):
            row = env.only_row(action)
            _assert_attributed(row, actor=_ADMIN, source="mcp")
            assert row.target_id == created["key_id"]
        assert created["api_key"] not in env.store.all_raw_text()


# =========================================================================
# MCP credentials
# =========================================================================


class TestMcpCredentials:
    def test_rest_self_and_admin(self, env) -> None:
        own = env.rest("POST", "/api/mcp-credentials", json={"name": "free text"})
        assert own.status_code == 201, own.text
        own_id = own.json()["credential_id"]
        other = env.rest(
            "POST", f"/api/admin/users/{_OTHER}/mcp-credentials", json={"name": None}
        )
        assert other.status_code == 201, other.text
        other_id = other.json()["credential_id"]
        env.rest("DELETE", f"/api/mcp-credentials/{own_id}")
        env.rest("DELETE", f"/api/admin/users/{_OTHER}/mcp-credentials/{other_id}")
        created = env.rows("mcp_credential_created")
        revoked = env.rows("mcp_credential_revoked")
        assert [(r.actor, r.target_id, r.source) for r in created] == [
            (_ADMIN, own_id, "rest"),
            (_ADMIN, other_id, "rest"),
        ]
        assert [r.details["for_self"] for r in created] == [True, False]
        assert [(r.actor, r.target_id, r.outcome) for r in revoked] == [
            (_ADMIN, own_id, "success"),
            (_ADMIN, other_id, "success"),
        ]
        text = env.store.all_raw_text()
        for secret in (own.json()["client_secret"], other.json()["client_secret"]):
            assert secret not in text

    def test_mcp_self_and_admin(self, env) -> None:
        own = env.mcp(
            "manage_mcp_credential", {"action": "create", "description": "free text"}
        )
        other = env.mcp(
            "manage_mcp_credential",
            {"action": "create", "target_user": _OTHER, "description": ""},
        )
        assert own["success"] and other["success"], (own, other)
        env.mcp(
            "manage_mcp_credential",
            {"action": "delete", "credential_id": own["credential_id"]},
        )
        env.mcp(
            "manage_mcp_credential",
            {
                "action": "delete",
                "target_user": _OTHER,
                "credential_id": other["credential_id"],
            },
        )
        created = env.rows("mcp_credential_created")
        revoked = env.rows("mcp_credential_revoked")
        assert [(r.actor, r.source) for r in created + revoked] == [(_ADMIN, "mcp")] * 4
        assert [r.details["for_self"] for r in created] == [True, False]
        assert own["client_secret"] not in env.store.all_raw_text()


# =========================================================================
# SSH keys
# =========================================================================


def _assert_ssh_rows(env: _Env, source: str, key_name: str) -> None:
    """One row per step, all naming the key by its fingerprint id."""
    rows = [
        env.only_row(action)
        for action in ("ssh_key_created", "ssh_key_host_assigned", "ssh_key_deleted")
    ]
    for row in rows:
        _assert_attributed(row, actor=_ADMIN, source=source)
        assert row.target_id.startswith("sha256:"), row.target_id
    assert len({row.target_id for row in rows}) == 1
    assert key_name not in env.store.all_raw_text()


class TestSshKeys:
    def test_rest(self, env) -> None:
        env.rest(
            "POST",
            "/api/ssh-keys",
            json={"name": "rest_key", "key_type": "ed25519", "description": "free"},
        )
        env.rest("POST", "/api/ssh-keys/rest_key/hosts", json={"hostname": _FORGE_HOST})
        env.rest("DELETE", "/api/ssh-keys/rest_key")
        _assert_ssh_rows(env, "rest", "rest_key")

    def test_mcp(self, env) -> None:
        env.mcp("manage_ssh_key", {"action": "create", "name": "mcp_key"})
        env.mcp(
            "manage_ssh_key",
            {"action": "assign_host", "name": "mcp_key", "hostname": _FORGE_HOST},
        )
        env.mcp("manage_ssh_key", {"action": "delete", "name": "mcp_key"})
        _assert_ssh_rows(env, "mcp", "mcp_key")

    def test_web(self, env) -> None:
        env.web(
            "POST",
            "/admin/ssh-keys/create",
            data={"key_name": "web_key", "key_type": "ed25519"},
        )
        env.web(
            "POST",
            "/admin/ssh-keys/assign-host",
            data={"key_name": "web_key", "hostname": _FORGE_HOST},
        )
        env.web("POST", "/admin/ssh-keys/delete", data={"key_name": "web_key"})
        _assert_ssh_rows(env, "web", "web_key")


# =========================================================================
# Git credentials
# =========================================================================


_ADD_BODY = {"forge_type": "github", "forge_host": _FORGE_HOST, "token": _TOKEN}


class TestGitCredentials:
    def _assert_pair(self, env: _Env, source: str) -> None:
        configured = env.only_row("git_credential_configured")
        deleted = env.only_row("git_credential_deleted")
        for row in (configured, deleted):
            _assert_attributed(row, actor=_ADMIN, source=source)
            assert row.details == {"platform": "github", "forge_host": _FORGE_HOST}
        assert configured.target_id == deleted.target_id
        assert _TOKEN not in env.store.all_raw_text()

    def test_mcp(self, env) -> None:
        added = env.mcp("configure_git_credential", _ADD_BODY)
        assert added["success"] is True, added
        env.mcp("delete_git_credential", {"credential_id": added["credential_id"]})
        self._assert_pair(env, "mcp")

    @pytest.mark.parametrize("prefix", ["/admin", "/user"])
    def test_web(self, env, prefix: str) -> None:
        added = env.web("POST", f"{prefix}/git-credentials", json=_ADD_BODY)
        assert added.status_code == 200, added.text
        credential_id = added.json()["credential_id"]
        env.web("DELETE", f"{prefix}/git-credentials/{credential_id}")
        self._assert_pair(env, "web")


# =========================================================================
# Group tool access (REST only) and groups / impersonation parity
# =========================================================================


class TestPermissions:
    def test_rest_tool_access_grant_revoke_bulk(self, env) -> None:
        group = env.groups.create_group(name="example-group", description="")
        base = "/api/v1/groups/tool-access"
        assert env.rest("POST", f"{base}/{group.id}/search_code").status_code == 200
        assert env.rest("DELETE", f"{base}/{group.id}/search_code").status_code == 200
        assert env.rest("POST", f"{base}/search_code/bulk-disable").status_code == 200
        granted = env.only_row("group_tool_access_granted")
        _assert_attributed(granted, actor=_ADMIN, source="rest")
        assert granted.target_id == str(group.id)
        revoked = env.rows("group_tool_access_revoked")
        assert [r.details["all_groups"] for r in revoked][0] is False
        assert all(r.details["all_groups"] for r in revoked[1:])
        assert len(revoked) == 1 + len(env.groups.get_all_groups())
        # One row per change: the legacy door-level tool rows are gone.
        assert env.store.rows("tool_access_") == []

    def test_group_create_on_every_door(self, env) -> None:
        env.rest(
            "POST", "/api/v1/groups", json={"name": "rest-group", "description": ""}
        )
        env.mcp("create_group", {"name": "mcp-group", "description": ""})
        env.web(
            "POST",
            "/admin/groups/create",
            data={"name": "web-group", "description": ""},
        )
        rows = env.rows("group_create")
        assert [(r.actor, r.source) for r in rows] == [
            (_ADMIN, "rest"),
            (_ADMIN, "mcp"),
            (_ADMIN, "web"),
        ]

    def test_impersonation_actor_is_the_admin_never_the_target(self, env) -> None:
        session_id = f"example-session-{uuid.uuid4().hex[:8]}"
        env.mcp("set_session_impersonation", {"username": _OTHER}, session_id)
        rows = env.store.rows("impersonation_")
        assert [(r.action_type, r.actor, r.target_id, r.source) for r in rows] == [
            ("impersonation_set", _ADMIN, _OTHER, "mcp"),
        ]
