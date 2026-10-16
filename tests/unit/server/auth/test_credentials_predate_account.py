"""Credentials issued to an account never authenticate a later account that
reuses its name -- including after a sliding refresh.

Each door's shared authentication dependency is exercised through HTTP on a
minimal app: REST bearer and the JWT cookie (``get_current_user``), the Web
session cookie (``get_current_user_hybrid``, ``get_current_user_web_or_api``),
MCP (``get_current_user_for_mcp``) and the real ``/mcp-public`` route, plus
the Web session sliding refresh (``get_and_refresh_session``).  Accounts,
JWTs and Web sessions are the real production classes over real SQLite
stores.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Tuple

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import mcp_router
from code_indexer.server.web import auth as web_auth
from tests.unit.server._account_rows import (
    OTHER_PASSWORD,
    PASSWORD,
    Stores,
    build_stores,
)

DOORS = ("/rest", "/web-hybrid", "/web-or-api", "/mcp-door")
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

Server = Tuple[TestClient, Stores, JWTManager, web_auth.SessionManager]


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    stores = build_stores(tmp_path)
    # Long lifetime: an aged (refresh-eligible) token stays valid for 30
    # minutes, even when the first request pays the server's cold start.
    jwt_manager = JWTManager(
        secret_key="example-secret-key-for-tests", token_expiration_minutes=120
    )
    sessions = web_auth.SessionManager(
        "example-session-secret", SimpleNamespace(host="127.0.0.1")
    )
    monkeypatch.setattr(dependencies, "user_manager", stores.user_manager)
    monkeypatch.setattr(dependencies, "jwt_manager", jwt_manager)
    monkeypatch.setattr(dependencies, "oauth_manager", None)
    monkeypatch.setattr(dependencies, "server_config", None)
    monkeypatch.setattr(web_auth, "_session_manager", sessions)

    app = FastAPI()
    app.include_router(mcp_router)

    @app.get("/rest")
    def rest(user: User = Depends(dependencies.get_current_user)) -> str:
        return user.username

    @app.get("/web-hybrid")
    def web_hybrid(user: User = Depends(dependencies.get_current_user_hybrid)) -> str:
        return user.username

    @app.get("/web-or-api")
    def web_or_api(
        user: User = Depends(dependencies.get_current_user_web_or_api),
    ) -> str:
        return user.username

    @app.get("/mcp-door")
    def mcp(user: User = Depends(dependencies.get_current_user_for_mcp)) -> str:
        return user.username

    @app.get("/web-refresh")
    def web_refresh(request: Request, response: Response) -> bool:
        return sessions.get_and_refresh_session(request, response) is not None

    yield TestClient(app), stores, jwt_manager, sessions


def _web_session(sessions: web_auth.SessionManager, username: str) -> str:
    response = Response()
    sessions.create_session(response, username, "admin")
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


def _aged_web_session(
    sessions: web_auth.SessionManager, username: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """A Web session created three quarters of its lifetime ago."""
    past = time.time() - web_auth.SESSION_TIMEOUT_SECONDS * 0.75
    with monkeypatch.context() as patch:
        patch.setattr(web_auth.time, "time", lambda: past)
        return _web_session(sessions, username)


def _aged_token(jwt_manager: JWTManager, username: str) -> str:
    """A JWT issued three quarters of its lifetime ago (refresh-eligible)."""
    lifetime = jwt_manager.token_expiration_minutes * 60
    issued = time.time() - lifetime * 0.75
    payload: Dict[str, Any] = {
        "username": username,
        "role": "admin",
        "created_at": None,
        "iat": issued,
        "exp": issued + lifetime,
        "jti": str(uuid.uuid4()),
    }
    return str(
        jose_jwt.encode(
            payload, jwt_manager.secret_key, algorithm=jwt_manager.algorithm
        )
    )


def _status(
    client: TestClient, door: str, *, bearer: str = "", session: str = ""
) -> int:
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    if session:
        client.cookies.set(web_auth.SESSION_COOKIE_NAME, session)
    return client.get(door, headers=headers).status_code


def _mcp_public_refresh(client: TestClient, cookie: str) -> str:
    """POST /mcp-public with *cookie*; returns the re-issued cookie or ''."""
    client.cookies.clear()
    client.cookies.set(dependencies.CIDX_SESSION_COOKIE, cookie)
    response = client.post("/mcp-public", json=TOOLS_LIST)
    prefix = f"{dependencies.CIDX_SESSION_COOKIE}="
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(prefix):
            return header[len(prefix) :].split(";", 1)[0]
    return ""


def _backdate_account(stores: Stores, username: str) -> None:
    """Record *username*'s account as created a day ago, before any aged
    token, so its own aged cookies are legitimately newer than the account."""
    created = datetime.now(timezone.utc) - timedelta(days=1)
    db = stores.server_dir / "data" / "cidx_server.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "UPDATE users SET account_created_at = ? WHERE username = ?",
            (created.isoformat(), username),
        )
        conn.commit()


def _jwt_cookie_status(client: TestClient, cookie: str) -> int:
    client.cookies.clear()
    client.cookies.set(dependencies.CIDX_SESSION_COOKIE, cookie)
    return client.get("/rest").status_code


def _recreate_alice(stores: Stores) -> None:
    assert stores.user_manager.delete_user_audited("alice", actor="admin")
    stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)


@pytest.mark.parametrize("door", list(DOORS))
def test_bearer_token_of_earlier_account_is_rejected(server, door: str) -> None:
    client, stores, jwt_manager, _ = server
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    old = jwt_manager.create_token({"username": "alice", "role": "admin"})
    assert _status(client, door, bearer=old) == 200

    _recreate_alice(stores)
    new = jwt_manager.create_token({"username": "alice", "role": "normal_user"})

    assert _status(client, door, bearer=old) == 401
    assert _status(client, door, bearer=new) == 200


def test_jwt_cookie_of_earlier_account_is_rejected(server) -> None:
    client, stores, jwt_manager, _ = server
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    old = jwt_manager.create_token({"username": "alice", "role": "admin"})
    _recreate_alice(stores)

    assert _jwt_cookie_status(client, old) == 401


@pytest.mark.parametrize("door", ["/web-hybrid", "/web-or-api"])
def test_web_session_of_earlier_account_is_rejected(server, door: str) -> None:
    client, stores, _, sessions = server
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    old = _web_session(sessions, "alice")
    assert _status(client, door, session=old) == 200

    _recreate_alice(stores)
    new = _web_session(sessions, "alice")

    assert _status(client, door, session=old) == 401
    assert _status(client, door, session=new) == 200


def test_account_without_recorded_creation_keeps_older_credentials(server) -> None:
    """Accounts created before the creation instant was recorded (NULL) keep
    accepting their credentials."""
    client, stores, jwt_manager, sessions = server
    old_token = jwt_manager.create_token({"username": "alice", "role": "admin"})
    old_session = _web_session(sessions, "alice")
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    with sqlite3.connect(stores.server_dir / "data" / "cidx_server.db") as conn:
        conn.execute("UPDATE users SET account_created_at = NULL")

    for door in DOORS:
        assert _status(client, door, bearer=old_token) == 200, door
    assert _status(client, "/web-hybrid", session=old_session) == 200


class TestSlidingRefreshKeepsOriginalAuthentication:
    def test_token_reissued_after_recreation_keeps_its_original_authentication(
        self, server
    ) -> None:
        """Re-issuing a token (expiry extension, cookie refresh) keeps the
        instant of the original authentication, so an earlier account's token
        re-issued after the name was re-created is still refused."""
        client, stores, jwt_manager, _ = server
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        old = jwt_manager.create_token({"username": "alice", "role": "admin"})
        _recreate_alice(stores)

        extended = jwt_manager.extend_token_expiration(old)
        response = Response()
        dependencies._refresh_jwt_cookie(response, jwt_manager.validate_token(old))
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        refreshed = cookie[dependencies.CIDX_SESSION_COOKIE].value

        assert _jwt_cookie_status(client, refreshed) == 401
        assert _status(client, "/rest", bearer=extended) == 401

    @pytest.fixture
    def live_alice(self, server: Server) -> Server:
        """A live alice whose account predates any aged token."""
        _, stores, _, _ = server
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        _backdate_account(stores, "alice")
        return server

    def test_live_accounts_aged_cookie_is_refreshed(self, live_alice: Server) -> None:
        """Positive control: a live account's own aged cookie is re-issued."""
        client, _, jwt_manager, _ = live_alice

        refreshed = _mcp_public_refresh(client, _aged_token(jwt_manager, "alice"))
        assert refreshed != ""

    def test_deleted_accounts_cookie_is_never_refreshed(
        self, live_alice: Server
    ) -> None:
        """Differs from the control only by deleting and re-creating the name."""
        client, stores, jwt_manager, _ = live_alice
        old = _aged_token(jwt_manager, "alice")
        _recreate_alice(stores)

        refreshed = _mcp_public_refresh(client, old)
        assert refreshed == ""

    def test_logged_out_cookie_is_never_refreshed(self, live_alice: Server) -> None:
        """Differs from the control only by revoking the cookie (logout)."""
        from code_indexer.server.app import blacklist_token

        client, _, jwt_manager, _ = live_alice
        cookie = _aged_token(jwt_manager, "alice")
        blacklist_token(jwt_manager.validate_token(cookie)["jti"])

        refreshed = _mcp_public_refresh(client, cookie)
        assert refreshed == ""

    def test_web_session_refreshed_after_recreation_is_refused(
        self, server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sliding Web session refresh keeps the original sign-in instant,
        so an earlier account's session refreshed after the name was
        re-created is still refused."""
        client, stores, _, sessions = server
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        old = _aged_web_session(sessions, "alice", monkeypatch)
        _recreate_alice(stores)

        client.cookies.clear()
        client.cookies.set(web_auth.SESSION_COOKIE_NAME, old)
        refreshed = client.get("/web-refresh").cookies.get(web_auth.SESSION_COOKIE_NAME)
        assert refreshed

        assert _status(client, "/web-hybrid", session=refreshed) == 401
        assert _status(client, "/web-or-api", session=refreshed) == 401
