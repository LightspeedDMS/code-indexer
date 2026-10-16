"""Completing an MFA challenge signs in only the live account that passed
the first factor -- never an account created under the same name after it.

Front door: the real app over an isolated server home with its real TOTP
service; the password step runs through ``POST /login`` (Web) and
``POST /oauth/authorize`` (OAuth), the second factor through
``POST /admin/mfa/challenge/verify`` and ``POST /oauth/mfa/verify``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Iterator, Tuple
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import auth as web_auth
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-MFA-Account-Passw0rd!"
REDIRECT_URI = "https://example.com/callback"
_CHALLENGE = re.compile(r"name=['\"]challenge_token['\"] value=['\"]([^'\"]+)")
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    root = tmp_path_factory.mktemp("mfa-account-app")
    from code_indexer.server.auth.totp_service import TOTPService
    from code_indexer.server.web import mfa_routes

    previous_totp = mfa_routes._totp_service
    try:
        with isolated_app(root) as app:
            # The TOTP service start-up wires in production (lifespan).
            mfa_routes.set_totp_service(TOTPService(db_path=str(root / "mfa.db")))
            yield TestClient(app, follow_redirects=False)
    finally:
        mfa_routes._totp_service = previous_totp


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


def _enrolled_member(accounts: UserManager) -> Tuple[str, pyotp.TOTP]:
    """A fresh account with TOTP enrolled through the real TOTP service."""
    from code_indexer.server.web.mfa_routes import get_totp_service

    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    totp_service = get_totp_service()
    assert totp_service is not None
    totp_service.generate_secret(name)
    uri = totp_service.get_provisioning_uri(name)
    assert uri is not None
    totp = pyotp.TOTP(parse_qs(urlparse(uri).query)["secret"][0])
    assert totp_service.activate_mfa(name, totp.at(int(time.time()) - 30))
    return name, totp


def _record_account_created_later(accounts: UserManager, name: str) -> None:
    """The account now dates from after the first factor that was passed."""
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    backend = accounts._sqlite_backend
    assert backend is not None
    db_path = backend._conn_manager.db_path
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE users SET account_created_at = ? WHERE username = ?",
            (later.isoformat(), name),
        )
        conn.commit()
    assert accounts.get_user(name).account_created_at == later  # type: ignore[union-attr]


def _web_challenge(client: TestClient, name: str) -> str:
    client.cookies.clear()
    match = _CSRF.search(client.get("/login").text)
    assert match, "csrf token not found on the login page"
    response = client.post(
        "/login",
        data={"username": name, "password": PASSWORD, "csrf_token": match.group(1)},
    )
    challenge = _CHALLENGE.search(response.text)
    assert challenge, response.text[:300]
    return challenge.group(1)


def _web_complete(client: TestClient, challenge: str, totp: pyotp.TOTP):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    return client.post(
        "/admin/mfa/challenge/verify",
        data={"challenge_token": challenge, "totp_code": totp.now()},
    )


def _session_cookie_set(response) -> bool:  # type: ignore[no-untyped-def]
    prefix = f"{web_auth.SESSION_COOKIE_NAME}="
    return any(h.startswith(prefix) for h in response.headers.get_list("set-cookie"))


def _oauth_challenge(client: TestClient, name: str) -> str:
    registered = client.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert registered.status_code == 201, registered.text
    verifier = secrets.token_urlsafe(48)
    challenge_value = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    client.cookies.clear()
    response = client.post(
        "/oauth/authorize",
        json={
            "client_id": registered.json()["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge_value,
            "username": name,
            "password": PASSWORD,
        },
    )
    challenge = _CHALLENGE.search(response.text)
    assert challenge, response.text[:300]
    return challenge.group(1)


def _oauth_complete(client: TestClient, challenge: str, totp: pyotp.TOTP):  # type: ignore[no-untyped-def]
    return client.post(
        "/oauth/mfa/verify",
        data={"challenge_token": challenge, "totp_code": totp.now()},
    )


def test_web_mfa_completion_signs_in_the_live_account(client, accounts) -> None:
    name, totp = _enrolled_member(accounts)

    response = _web_complete(client, _web_challenge(client, name), totp)

    assert response.status_code == 303, response.text
    assert _session_cookie_set(response)


def test_web_mfa_completion_refuses_an_account_created_after_the_first_factor(
    client, accounts
) -> None:
    name, totp = _enrolled_member(accounts)
    challenge = _web_challenge(client, name)
    _record_account_created_later(accounts, name)

    response = _web_complete(client, challenge, totp)

    assert response.headers.get("location", "").startswith("/login")
    assert not _session_cookie_set(response)


def test_oauth_mfa_completion_issues_a_code_for_the_live_account(
    client, accounts
) -> None:
    name, totp = _enrolled_member(accounts)

    response = _oauth_complete(client, _oauth_challenge(client, name), totp)

    assert response.status_code == 302, response.text
    assert "code=" in response.headers["location"]


def test_oauth_mfa_completion_refuses_an_account_created_after_the_first_factor(
    client, accounts
) -> None:
    name, totp = _enrolled_member(accounts)
    challenge = _oauth_challenge(client, name)
    _record_account_created_later(accounts, name)

    response = _oauth_complete(client, challenge, totp)

    assert "code=" not in response.headers.get("location", "")
    assert response.status_code in (400, 401), response.text
