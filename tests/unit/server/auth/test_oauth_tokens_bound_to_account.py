"""An OAuth credential authenticates, and is issued, only for the live
account it belongs to -- never for a removed account or for a later account
created under the same name.

Front door: the real app (``create_app``) over an isolated server home, the
real OAuth flow (``/oauth/register``, ``/oauth/authorize/consent`` with a real
Web session, ``/oauth/token``) and a Bearer call to ``/api/repos``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Dict, Iterator, Tuple

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import auth as web_auth
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-OAuth-Account-Passw0rd!"
REDIRECT_URI = "https://example.com/callback"
BEARER_PAGE = "/api/repos"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("oauth-account-app")) as app:
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


@pytest.fixture
def oauth_client_id(client: TestClient) -> str:
    response = client.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert response.status_code == 201, response.text
    client_id: str = response.json()["client_id"]
    return client_id


def _member(accounts: UserManager) -> str:
    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    return name


def _users_db(accounts: UserManager) -> str:
    backend = accounts._sqlite_backend
    assert backend is not None
    path: str = backend._conn_manager.db_path
    return path


def _record_account_created(accounts: UserManager, name: str, when: datetime) -> None:
    with closing(sqlite3.connect(_users_db(accounts))) as conn:
        conn.execute(
            "UPDATE users SET account_created_at = ? WHERE username = ?",
            (when.isoformat(), name),
        )
        conn.commit()
    assert accounts.get_user(name).account_created_at == when  # type: ignore[union-attr]


def _remove_account_row(accounts: UserManager, name: str) -> None:
    """Only the account row: the state between removing an account and
    purging the rows keyed to its name."""
    with closing(sqlite3.connect(_users_db(accounts))) as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (name,))
        conn.commit()
    assert accounts.get_user(name) is None


def _web_session(name: str) -> str:
    response = Response()
    web_auth.get_session_manager().create_session(response, name, "normal_user")
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


def _consent(client: TestClient, client_id: str, session: str) -> Tuple[str, str]:
    """Run the consent step; return (redirect location, PKCE verifier)."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, session)
    response = client.post(
        "/oauth/authorize/consent",
        data={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "response_type": "code",
            "consent": "allow",
        },
    )
    client.cookies.clear()
    assert response.status_code in (302, 303), response.text
    return response.headers["location"], verifier


def _code(client: TestClient, client_id: str, name: str) -> Tuple[str, str]:
    location, verifier = _consent(client, client_id, _web_session(name))
    assert location.startswith(REDIRECT_URI), location
    return location.split("code=")[1].split("&")[0], verifier


def _exchange(client: TestClient, client_id: str, code: str, verifier: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )


def _refresh(client: TestClient, client_id: str, refresh_token: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def _tokens(client: TestClient, client_id: str, name: str) -> Dict[str, str]:
    response = _exchange(client, client_id, *_code(client, client_id, name))
    assert response.status_code == 200, response.text
    tokens: Dict[str, str] = response.json()
    return tokens


def _bearer_status(client: TestClient, access_token: str) -> int:
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {access_token}"}
    return client.get(BEARER_PAGE, headers=headers).status_code


def test_live_accounts_token_is_accepted_and_refreshed(
    client, accounts, oauth_client_id
) -> None:
    name = _member(accounts)
    tokens = _tokens(client, oauth_client_id, name)

    assert _bearer_status(client, tokens["access_token"]) == 200
    refreshed = _refresh(client, oauth_client_id, tokens["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    assert _bearer_status(client, refreshed.json()["access_token"]) == 200


def test_token_issued_before_the_account_was_created_is_refused(
    client, accounts, oauth_client_id
) -> None:
    name = _member(accounts)
    tokens = _tokens(client, oauth_client_id, name)
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    _record_account_created(accounts, name, later)

    assert _bearer_status(client, tokens["access_token"]) == 401
    _assert_invalid_grant(_refresh(client, oauth_client_id, tokens["refresh_token"]))


def _assert_invalid_grant(response) -> None:  # type: ignore[no-untyped-def]
    """HTTP 400 with the RFC 6749 error fields at the top level, no token."""
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "invalid_grant", body
    assert body["error_description"]
    assert "detail" not in body and "access_token" not in body


def test_code_issued_before_the_account_was_created_is_refused_at_exchange(
    client, accounts, oauth_client_id
) -> None:
    """The code dates from before the account, while a token minted at
    exchange would not: the code's own issuance instant decides."""
    name = _member(accounts)
    code, verifier = _code(client, oauth_client_id, name)
    _record_account_created(accounts, name, datetime.now(timezone.utc))

    _assert_invalid_grant(_exchange(client, oauth_client_id, code, verifier))


def test_code_of_a_deleted_and_recreated_account_is_refused(
    client, accounts, oauth_client_id
) -> None:
    name = _member(accounts)
    code, verifier = _code(client, oauth_client_id, name)
    assert accounts.delete_user_audited(name, actor="example-admin")
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)

    refused = _exchange(client, oauth_client_id, code, verifier)

    assert refused.status_code == 400, refused.text
    assert "access_token" not in refused.json()


def test_no_token_is_issued_once_the_account_is_gone(
    client, accounts, oauth_client_id
) -> None:
    holder, pending = _member(accounts), _member(accounts)
    tokens = _tokens(client, oauth_client_id, holder)
    code, verifier = _code(client, oauth_client_id, pending)
    _remove_account_row(accounts, holder)
    _remove_account_row(accounts, pending)

    _assert_invalid_grant(_exchange(client, oauth_client_id, code, verifier))
    _assert_invalid_grant(_refresh(client, oauth_client_id, tokens["refresh_token"]))


def test_client_credentials_grant_issues_no_token_once_the_account_is_gone(
    client, accounts
) -> None:
    name = _member(accounts)
    credential = MCPCredentialManager(user_manager=accounts).generate_credential(name)
    grant = {
        "grant_type": "client_credentials",
        "client_id": credential["client_id"],
        "client_secret": credential["client_secret"],
    }
    assert client.post("/oauth/token", data=grant).status_code == 200
    _remove_account_row(accounts, name)

    refused = client.post("/oauth/token", data=grant)

    assert refused.status_code in (400, 401), refused.text
    assert "access_token" not in refused.json()


def test_consent_with_a_deleted_accounts_session_issues_no_code(
    client, accounts, oauth_client_id
) -> None:
    name = _member(accounts)
    session = _web_session(name)
    assert accounts.delete_user_audited(name, actor="example-admin")

    location, _ = _consent(client, oauth_client_id, session)

    assert location.startswith("/login"), location
    assert "code=" not in location
