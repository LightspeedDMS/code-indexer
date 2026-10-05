"""Audit attribution during MCP session impersonation.

Invariant: audit records written during MCP impersonation name the
authenticated administrator as the actor and the impersonated user as the
subject (the ``impersonated_user`` column).  Without impersonation that
column is NULL.  The authenticated administrator can always clear
impersonation, whatever the impersonated user may call.

Front door: the real MCP JSON-RPC ``tools/call`` dispatcher with a real
session registry entry, real auth components on isolated files
(``self_service_elevation_harness``), a real group store and a real audit
store on SQLite bound as the process audit sink.  Elevation enforcement is
off at both read points.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import mcp_router
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
    bind_audit_request_context,
    build_request_context,
    reset_audit_request_context,
)
from tests.unit.server.self_service_elevation_harness import build_stack, enforcement

_ADMIN = "example-admin"
_SUBJECT = "example-subject"
_PASSWORD = "SecureP@ssw0rd!XyZ789"


class _Env:
    def __init__(
        self,
        store: AuditStore,
        admin: User,
        subject: User,
        groups: Any,
        stack: Any,
        client: Any,
    ) -> None:
        self.store = store
        self.admin = admin
        self.subject = subject
        self.groups = groups
        self.stack = stack
        self.client = client
        self.groups_db = Path(groups.db_path)
        self.session_id = f"example-session-{uuid.uuid4()}"
        self.session_ids = {self.session_id}

    def http(
        self, username: str, tool: str, arguments: Dict[str, Any], session_id: str
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """One tools/call through the real ``POST /mcp`` endpoint, with a
        freshly issued bearer token for *username* and the given session id.
        Returns (HTTP status, decoded tool payload or None)."""
        self.session_ids.add(session_id)
        user = self.stack.user_manager.get_user(username)
        assert user is not None
        token, _jti = self.stack.bearer(user)
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
        )
        if response.status_code != 200:
            return response.status_code, None
        body = response.json()
        assert "result" in body, body
        payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
        return 200, payload

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """One tools/call on the admin's MCP session, as the /mcp door."""
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
                    session_id=self.session_id,
                )
            )
        finally:
            reset_audit_request_context(token)
        assert "result" in response, response
        payload: Dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
        return payload

    def list_tools(self) -> List[str]:
        """tools/list on the admin's MCP session."""
        from code_indexer.server.mcp.protocol import process_jsonrpc_request

        response = asyncio.run(
            process_jsonrpc_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                self.admin,
                session_id=self.session_id,
            )
        )
        assert "result" in response, response
        return [tool["name"] for tool in response["result"]["tools"]]

    def rows(self, action_type: str) -> List[Tuple[str, Optional[str], str]]:
        """(actor, impersonated_user, source) of every row of *action_type*."""
        conn = sqlite3.connect(str(self.store.db_path))
        try:
            return [
                (r[0], r[1], r[2])
                for r in conn.execute(
                    "SELECT admin_id, impersonated_user, source FROM audit_logs "
                    "WHERE action_type = ? ORDER BY id",
                    (action_type,),
                ).fetchall()
            ]
        finally:
            conn.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch, home_in_tmp: Path) -> Iterator[_Env]:
    from code_indexer.server.auth.audit_logger import password_audit_logger

    server_dir = tmp_path / "server"
    (server_dir / "data").mkdir(parents=True)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.mcp.session_registry import get_session_registry
    from code_indexer.server.services.group_access_manager import GroupAccessManager

    stack = build_stack(tmp_path, monkeypatch)
    admin = stack.user_manager.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    subject = stack.user_manager.create_user(_SUBJECT, _PASSWORD, UserRole.NORMAL_USER)
    groups = GroupAccessManager(tmp_path / "groups.db")
    monkeypatch.setattr(app_module.app.state, "group_manager", groups, raising=False)

    for store in bound_audit_store(tmp_path / "audit.db"):
        monkeypatch.setattr(password_audit_logger, "_audit_service", store.service)
        monkeypatch.setattr(
            app_module.app.state, "audit_service", store.service, raising=False
        )
        app = FastAPI()
        app.add_middleware(AuditRequestContextMiddleware)
        app.include_router(mcp_router)
        client = TestClient(app, raise_server_exceptions=False)
        environment = _Env(store, admin, subject, groups, stack, client)
        try:
            with enforcement(False):
                yield environment
        finally:
            for session_id in environment.session_ids:
                get_session_registry().remove_session(session_id)


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    import logging

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


class TestActorDuringImpersonation:
    def test_audited_action_names_admin_as_actor_and_impersonated_user_as_subject(
        self, env
    ) -> None:
        assert env.call("set_session_impersonation", {"username": _SUBJECT}) == {
            "status": "ok",
            "impersonating": _SUBJECT,
        }

        created = env.call("create_api_key", {"description": "example key"})

        assert created["success"] is True, created
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]

    def test_no_call_leaves_its_principal_behind(self, env) -> None:
        """The calls of one JSON-RPC batch run in one context: the principal a
        call binds ends with it, whether it ran or was refused."""
        from code_indexer.server.mcp.protocol import handle_tools_call
        from code_indexer.server.middleware.audit_request_context import (
            current_mcp_principal,
        )

        env.call("set_session_impersonation", {"username": _SUBJECT})

        async def batch() -> List[Any]:
            seen = []
            await handle_tools_call(
                {"name": "create_api_key", "arguments": {"description": "key"}},
                env.admin,
                session_id=env.session_id,
            )
            seen.append(current_mcp_principal())
            with pytest.raises(ValueError, match="Unknown tool"):
                await handle_tools_call(
                    {"name": "no_such_tool"}, env.admin, session_id=env.session_id
                )
            seen.append(current_mcp_principal())
            return seen

        token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
        try:
            assert asyncio.run(batch()) == [None, None]
        finally:
            reset_audit_request_context(token)
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]

    def test_without_impersonation_subject_is_null(self, env) -> None:
        created = env.call("create_api_key", {"description": "example key"})

        assert created["success"] is True, created
        assert env.rows("api_key_created") == [(_ADMIN, None, "mcp")]


def _tool_request(request_id: int, tool: str, arguments: Dict[str, Any]) -> Any:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def _batch_with_held_first_call(
    env: _Env, monkeypatch, later_call: Dict[str, Any]
) -> Dict[str, Any]:
    """Run a JSON-RPC batch on the admin's session: ``create_api_key`` (held
    past its handler timeout, so its worker thread is abandoned), then
    ``set_session_impersonation(later_call)``, whose payload is returned,
    then an ordinary call made under the session's new state.  The held call
    is released only after the whole batch has returned, and runs to
    completion before this helper returns."""
    from code_indexer.server.mcp import protocol
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    real = HANDLER_REGISTRY["create_api_key"]
    release = threading.Event()

    @functools.wraps(real)
    def held(*args: Any, **kwargs: Any) -> Any:
        if not release.wait(timeout=30):
            raise AssertionError("held call was never released")
        return real(*args, **kwargs)

    resolve_timeout = protocol._resolve_handler_timeout
    monkeypatch.setitem(HANDLER_REGISTRY, "create_api_key", held)
    monkeypatch.setattr(
        protocol,
        "_resolve_handler_timeout",
        lambda name: 0.2 if name == "create_api_key" else resolve_timeout(name),
    )
    loop = asyncio.new_event_loop()
    token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
    try:
        responses = loop.run_until_complete(
            protocol.process_batch_request(
                [
                    _tool_request(1, "create_api_key", {"description": "example"}),
                    _tool_request(2, "set_session_impersonation", later_call),
                    _tool_request(3, "get_tool_categories", {}),
                ],
                env.admin,
                session_id=env.session_id,
            )
        )
    finally:
        reset_audit_request_context(token)
        release.set()
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
    # A timed-out call's result is the dispatcher's own error dict.
    assert "timed out" in responses[0]["result"]["error"], responses[0]
    assert "content" in responses[2].get("result", {}), responses[2]
    later: Dict[str, Any] = json.loads(responses[1]["result"]["content"][0]["text"])
    return later


class TestPerCallAttribution:
    """Audit attribution is fixed per tool call: a call's worker thread that
    outlives its timeout writes its rows with ITS principal, whatever later
    calls of the same JSON-RPC batch change."""

    def test_timed_out_call_keeps_its_principal_after_a_later_call_clears_impersonation(
        self, env, monkeypatch
    ) -> None:
        env.call("set_session_impersonation", {"username": _SUBJECT})

        later = _batch_with_held_first_call(env, monkeypatch, {})

        assert later == {"status": "ok", "impersonating": None}
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]

    def test_timed_out_call_does_not_pick_up_a_later_calls_subject(
        self, env, monkeypatch
    ) -> None:
        later = _batch_with_held_first_call(env, monkeypatch, {"username": _SUBJECT})

        assert later == {"status": "ok", "impersonating": _SUBJECT}
        assert env.rows("api_key_created") == [(_ADMIN, None, "mcp")]


_CONCURRENT_CALLS = 1000


class TestOneEffectiveUserPerCall:
    """A call's audit principal and the user its handler runs as come from
    ONE read of the session, so a concurrent impersonation change can never
    split them."""

    @staticmethod
    def _recording_handler(monkeypatch) -> List[Tuple[Optional[str], str]]:
        """Replace the ``create_api_key`` handler with one recording, per
        call, the bound principal's subject and the user it received."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
        from code_indexer.server.middleware.audit_request_context import (
            current_mcp_principal,
        )

        seen: List[Tuple[Optional[str], str]] = []

        async def recorder(args: Dict[str, Any], user: User) -> Dict[str, Any]:
            principal = current_mcp_principal()
            subject = None if principal is None else principal.impersonated_user
            seen.append((subject, user.username))
            return {"success": True}

        monkeypatch.setitem(HANDLER_REGISTRY, "create_api_key", recorder)
        return seen

    @staticmethod
    def _split(seen: List[Tuple[Optional[str], str]]) -> List[Any]:
        """Calls whose bound subject is not the user they ran as."""
        return [
            (subject, ran_as)
            for subject, ran_as in seen
            if subject != (None if ran_as == _ADMIN else ran_as)
        ]

    def test_switch_right_after_the_session_read_cannot_split_the_call(
        self, env, monkeypatch
    ) -> None:
        from code_indexer.server.auth.mcp_session_state import MCPSessionState
        from code_indexer.server.mcp.protocol import handle_tools_call

        class ClearedRightAfterFirstRead(MCPSessionState):
            """A concurrent clear lands right after the dispatcher's read."""

            @property
            def effective_user(self) -> User:
                current = super().effective_user
                self.clear_impersonation()
                return current

        session = ClearedRightAfterFirstRead("example-session-split", env.admin)
        session.set_impersonation(env.subject)
        seen = self._recording_handler(monkeypatch)

        asyncio.run(
            handle_tools_call(
                {"name": "create_api_key", "arguments": {}},
                env.admin,
                session_state=session,
            )
        )

        assert seen == [(_SUBJECT, _SUBJECT)]

    def test_concurrent_switching_never_splits_a_call(self, env, monkeypatch) -> None:
        import sys

        from code_indexer.server.auth.mcp_session_state import MCPSessionState
        from code_indexer.server.mcp.protocol import handle_tools_call

        session = MCPSessionState("example-session-concurrent", env.admin)
        seen = self._recording_handler(monkeypatch)
        stop = threading.Event()

        def toggle() -> None:
            while not stop.is_set():
                session.set_impersonation(env.subject)
                session.clear_impersonation()

        async def dispatch() -> None:
            for _ in range(_CONCURRENT_CALLS):
                await handle_tools_call(
                    {"name": "create_api_key", "arguments": {}},
                    env.admin,
                    session_state=session,
                )

        previous_interval = sys.getswitchinterval()
        toggler = threading.Thread(target=toggle, daemon=True)
        sys.setswitchinterval(1e-6)
        toggler.start()
        try:
            asyncio.run(dispatch())
        finally:
            stop.set()
            toggler.join(timeout=10)
            sys.setswitchinterval(previous_interval)

        assert not toggler.is_alive()
        assert len(seen) == _CONCURRENT_CALLS
        assert self._split(seen) == []


_DEMOTED = "example-demoted"
_OTHER = "example-other"


class TestSessionBinding:
    """Through the real ``POST /mcp`` endpoint and session registry.

    Impersonation is authorized against the CURRENT authenticated caller,
    never a role stored when the MCP session was created, and a session id
    is bound to the user it was created for."""

    def _demoted_admin_session(self, env) -> str:
        env.stack.user_manager.create_user(_DEMOTED, _PASSWORD, UserRole.ADMIN)
        session_id = f"example-session-{uuid.uuid4()}"
        status, payload = env.http(_DEMOTED, "create_api_key", {}, session_id)
        assert status == 200 and payload is not None and payload["success"]
        return session_id

    def test_demoted_admin_cannot_set_impersonation_on_existing_session(
        self, env
    ) -> None:
        session_id = self._demoted_admin_session(env)
        env.stack.user_manager.update_user_role(_DEMOTED, UserRole.NORMAL_USER)

        status, payload = env.http(
            _DEMOTED, "set_session_impersonation", {"username": _SUBJECT}, session_id
        )

        assert status == 200
        assert payload == {
            "status": "error",
            "error": "Impersonation requires ADMIN role",
        }
        assert env.rows("impersonation_set") == []

    def test_demoted_admin_does_not_keep_impersonation(self, env) -> None:
        session_id = self._demoted_admin_session(env)
        status, payload = env.http(
            _DEMOTED, "set_session_impersonation", {"username": _SUBJECT}, session_id
        )
        assert (status, payload) == (
            200,
            {"status": "ok", "impersonating": _SUBJECT},
        )
        env.stack.user_manager.update_user_role(_DEMOTED, UserRole.NORMAL_USER)

        status, payload = env.http(_DEMOTED, "create_api_key", {}, session_id)

        assert status == 200 and payload is not None and payload["success"]
        assert env.rows("api_key_created")[-1] == (_DEMOTED, None, "mcp")

    def test_another_users_session_id_is_refused_and_owner_keeps_it(self, env) -> None:
        env.stack.user_manager.create_user(_OTHER, _PASSWORD, UserRole.NORMAL_USER)
        session_id = f"example-session-{uuid.uuid4()}"
        assert env.http(
            _ADMIN, "set_session_impersonation", {"username": _SUBJECT}, session_id
        ) == (200, {"status": "ok", "impersonating": _SUBJECT})

        status, payload = env.http(_OTHER, "create_api_key", {}, session_id)

        assert (status, payload) == (404, None)
        assert env.rows("api_key_created") == []
        status, payload = env.http(_ADMIN, "create_api_key", {}, session_id)
        assert status == 200 and payload is not None and payload["success"]
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]

    def test_refused_session_id_attempt_logs_one_warning_without_identities(
        self, env, caplog
    ) -> None:
        import hashlib
        import logging

        registry_logger = "code_indexer.server.mcp.session_registry"
        caplog.set_level(logging.WARNING, logger=registry_logger)
        env.stack.user_manager.create_user(_OTHER, _PASSWORD, UserRole.NORMAL_USER)
        session_id = f"example-session-{uuid.uuid4()}"
        assert env.http(_ADMIN, "create_api_key", {}, session_id)[0] == 200

        assert env.http(_OTHER, "create_api_key", {}, session_id) == (404, None)

        warnings = [
            r
            for r in caplog.records
            if r.name == registry_logger and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert hashlib.sha256(session_id.encode()).hexdigest()[:12] in message
        for identity in (session_id, _ADMIN, _OTHER):
            assert identity not in message

    @pytest.mark.parametrize("as_batch", [False, True], ids=["single", "batch"])
    def test_session_taken_over_after_the_owner_check_is_refused_as_not_found(
        self, env, monkeypatch, as_batch: bool
    ) -> None:
        """The session is evicted and recreated by another account between
        the endpoint's owner check and the dispatcher's session lookup: the
        request is refused exactly as the endpoint's own check refuses it."""
        from code_indexer.server.mcp.session_registry import get_session_registry

        other = env.stack.user_manager.create_user(
            _OTHER, _PASSWORD, UserRole.NORMAL_USER
        )
        session_id = f"example-session-{uuid.uuid4()}"
        env.session_ids.add(session_id)
        registry = get_session_registry()
        real_lookup = registry.get_or_create_session
        lookups: List[str] = []

        def taken_over_after_owner_check(sid: str, authenticated_user: Any) -> Any:
            if sid == session_id:
                lookups.append(sid)
                if len(lookups) == 2:
                    registry.remove_session(sid)
                    real_lookup(sid, other)
            return real_lookup(sid, authenticated_user)

        monkeypatch.setattr(
            registry, "get_or_create_session", taken_over_after_owner_check
        )
        token, _jti = env.stack.bearer(env.admin)
        request = _tool_request(1, "create_api_key", {})

        response = env.client.post(
            "/mcp",
            json=[request] if as_batch else request,
            headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
        )

        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": -32001,
            "message": "Session not found",
        }
        assert len(lookups) == 2
        assert env.rows("api_key_created") == []

    def test_same_user_reconnecting_keeps_its_session(self, env) -> None:
        session_id = f"example-session-{uuid.uuid4()}"
        env.http(
            _ADMIN, "set_session_impersonation", {"username": _SUBJECT}, session_id
        )

        status, payload = env.http(_ADMIN, "create_api_key", {}, session_id)

        assert status == 200 and payload is not None and payload["success"]
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]

    @staticmethod
    def _sqlite_accounts(env, tmp_path: Path, monkeypatch) -> Any:
        """Rebind the accounts to a real SQLite account store, which records
        each account's creation instant, and create the admin and subject."""
        from code_indexer.server.auth import dependencies
        from code_indexer.server.mcp.handlers._utils import app_module
        from tests.unit.server._account_rows import build_stores

        users = build_stores(tmp_path / "accounts").user_manager
        users.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
        users.create_user(_SUBJECT, _PASSWORD, UserRole.NORMAL_USER)
        monkeypatch.setattr(dependencies, "user_manager", users)
        monkeypatch.setattr(app_module, "user_manager", users)
        monkeypatch.setattr(env.stack, "user_manager", users)
        return users

    def test_recreated_account_does_not_inherit_the_earlier_accounts_session(
        self, env, tmp_path: Path, monkeypatch
    ) -> None:
        users = self._sqlite_accounts(env, tmp_path, monkeypatch)
        session_id = f"example-session-{uuid.uuid4()}"
        assert env.http(
            _ADMIN, "set_session_impersonation", {"username": _SUBJECT}, session_id
        ) == (200, {"status": "ok", "impersonating": _SUBJECT})
        assert users.delete_user_audited(_ADMIN, actor=_ADMIN)
        users.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)

        status, payload = env.http(_ADMIN, "create_api_key", {}, session_id)

        assert (status, payload) == (404, None)
        assert env.rows("api_key_created") == []
        from code_indexer.server.mcp.session_registry import get_session_registry

        kept = get_session_registry().get_session(session_id)
        assert kept is not None and kept.impersonated_user is not None
        recreated = users.get_user(_ADMIN)
        assert recreated is not None and recreated.account_created_at is not None
        assert kept.authenticated_user.account_created_at is not None
        assert kept.authenticated_user.account_created_at != (
            recreated.account_created_at
        )
        fresh = f"example-session-{uuid.uuid4()}"
        status, payload = env.http(_ADMIN, "create_api_key", {}, fresh)
        assert status == 200 and payload is not None and payload["success"]
        assert env.rows("api_key_created") == [(_ADMIN, None, "mcp")]

    def test_same_recorded_account_reconnecting_keeps_its_session(
        self, env, tmp_path: Path, monkeypatch
    ) -> None:
        self._sqlite_accounts(env, tmp_path, monkeypatch)
        session_id = f"example-session-{uuid.uuid4()}"
        assert env.http(
            _ADMIN, "set_session_impersonation", {"username": _SUBJECT}, session_id
        ) == (200, {"status": "ok", "impersonating": _SUBJECT})

        status, payload = env.http(_ADMIN, "create_api_key", {}, session_id)

        assert status == 200 and payload is not None and payload["success"]
        assert env.rows("api_key_created") == [(_ADMIN, _SUBJECT, "mcp")]


class TestReadPath:
    def test_mcp_audit_query_exposes_impersonated_user(self, env) -> None:
        env.call("set_session_impersonation", {"username": _SUBJECT})
        env.call("create_api_key", {"description": "example key"})
        env.call("set_session_impersonation", {})
        env.call("create_api_key", {"description": "example key"})

        result = env.call("query_audit_logs", {"action_type": "api_key_created"})

        assert result["success"] is True, result
        seen = sorted(
            (
                (entry["admin_id"], entry.get("impersonated_user", "<absent>"))
                for entry in result["entries"]
            ),
            key=str,
        )
        assert seen == sorted([(_ADMIN, None), (_ADMIN, _SUBJECT)], key=str)

    def test_web_audit_table_shows_the_impersonated_subject(self, env) -> None:
        from code_indexer.server.services.audit_log_query import (
            build_filters,
            query_audit_log,
        )
        from code_indexer.server.web.audit_log_routes import row_view, templates

        env.call("set_session_impersonation", {"username": _SUBJECT})
        env.call("create_api_key", {"description": "example key"})
        page = query_audit_log(
            env.store.service, build_filters(action_type="api_key_created")
        )
        views = [row_view(row) for row in page.rows]

        assert [v["actor"]["impersonated_user"] for v in views] == [_SUBJECT]
        html = templates.env.get_template("partials/audit_logs_table.html").render(
            rows=views,
            view="all",
            window_label="All time",
            total=1,
            total_capped=False,
            export_query="",
        )
        assert f"acting as {_SUBJECT}" in html


class TestClearingImpersonation:
    def test_admin_clears_impersonation_and_later_rows_have_no_subject(
        self, env
    ) -> None:
        env.call("set_session_impersonation", {"username": _SUBJECT})

        cleared = env.call("set_session_impersonation", {})

        assert cleared == {"status": "ok", "impersonating": None}
        assert env.rows("impersonation_denied") == []
        assert env.rows("impersonation_cleared") == [(_ADMIN, None, "mcp")]
        env.call("create_api_key", {"description": "example key"})
        assert env.rows("api_key_created") == [(_ADMIN, None, "mcp")]

    def test_clear_racing_a_concurrent_clear_still_succeeds(self, env) -> None:
        from code_indexer.server.auth.mcp_session_state import MCPSessionState
        from code_indexer.server.mcp.handlers.admin import (
            handle_set_session_impersonation,
        )

        class ClearedRightAfterCheck(MCPSessionState):
            """A concurrent clear lands right after the session is checked."""

            @property
            def is_impersonating(self) -> bool:
                current = super().is_impersonating
                self.clear_impersonation()
                return current

        session = ClearedRightAfterCheck("example-session-clear", env.admin)
        session.set_impersonation(env.subject)

        result = handle_set_session_impersonation({}, env.admin, session_state=session)

        assert json.loads(result["content"][0]["text"]) == {
            "status": "ok",
            "impersonating": None,
        }
        assert session.impersonated_user is None

    def test_switching_impersonation_records_the_admin_without_a_subject(
        self, env
    ) -> None:
        """set_session_impersonation is authorized for, and performed by, the
        authenticated administrator, so its rows never carry the subject the
        session impersonated before the call."""
        env.stack.user_manager.create_user(_OTHER, _PASSWORD, UserRole.NORMAL_USER)
        env.call("set_session_impersonation", {"username": _SUBJECT})

        switched = env.call("set_session_impersonation", {"username": _OTHER})

        assert switched == {"status": "ok", "impersonating": _OTHER}
        assert env.rows("impersonation_set") == [
            (_ADMIN, None, "mcp"),
            (_ADMIN, None, "mcp"),
        ]

    def test_admin_clears_impersonation_of_user_whose_group_lacks_the_tool(
        self, env
    ) -> None:
        """Group tool access is enforced; only the admin's group grants the
        impersonation tool.  Clearing is authorized for the authenticated
        administrator, so it is never blocked by the impersonated user's
        group."""
        _enforce_group_tool_access(env, admin_tools=["set_session_impersonation"])
        env.call("set_session_impersonation", {"username": _SUBJECT})

        cleared = env.call("set_session_impersonation", {})

        assert cleared == {"status": "ok", "impersonating": None}
        assert env.rows("impersonation_cleared") == [(_ADMIN, None, "mcp")]

    def test_impersonated_user_group_still_governs_other_tools(self, env) -> None:
        _enforce_group_tool_access(
            env, admin_tools=["set_session_impersonation", "create_api_key"]
        )
        env.call("set_session_impersonation", {"username": _SUBJECT})

        with pytest.raises(AssertionError, match="tool access denied"):
            env.call("create_api_key", {"description": "example key"})
        assert env.rows("api_key_created") == []

    def test_impersonation_tool_stays_listed_for_the_admin(self, env) -> None:
        _enforce_group_tool_access(
            env, admin_tools=["set_session_impersonation", "create_api_key"]
        )
        env.call("set_session_impersonation", {"username": _SUBJECT})

        listed = env.list_tools()

        assert "set_session_impersonation" in listed
        assert "create_api_key" not in listed

    def test_guides_list_the_impersonation_tool_like_tools_list(self, env) -> None:
        _enforce_group_tool_access(
            env,
            admin_tools=["set_session_impersonation"],
            subject_tools=("get_tool_categories", "cidx_quick_reference"),
        )
        env.call("set_session_impersonation", {"username": _SUBJECT})

        categories = env.call("get_tool_categories", {})
        doc = env.call("cidx_quick_reference", {"tool": "set_session_impersonation"})

        listed = [
            entry.split(" - ")[0]
            for tools in categories["categories"].values()
            for entry in tools
        ]
        assert "set_session_impersonation" in listed
        assert "create_api_key" not in listed
        assert doc["success"] is True, doc


def _enforce_group_tool_access(
    env: _Env, *, admin_tools: List[str], subject_tools: Tuple[str, ...] = ()
) -> None:
    """Turn group tool enforcement on: the admin's group grants *admin_tools*,
    the impersonated user's group grants *subject_tools* only."""
    from code_indexer.server.services.constants import (
        DEFAULT_GROUP_ADMINS,
        DEFAULT_GROUP_USERS,
    )

    admins = env.groups.get_group_by_name(DEFAULT_GROUP_ADMINS)
    users = env.groups.get_group_by_name(DEFAULT_GROUP_USERS)
    env.groups.assign_user_to_group(_ADMIN, admins.id, "test-setup")
    env.groups.assign_user_to_group(_SUBJECT, users.id, "test-setup")
    for tool in admin_tools:
        env.groups.set_tool_access(tool, admins.id, True, "test-setup")
    for tool in subject_tools:
        env.groups.set_tool_access(tool, users.id, True, "test-setup")
    conn = sqlite3.connect(str(env.groups_db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO tool_access_migration_state (id, complete) "
            "VALUES (1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
