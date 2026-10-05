"""The Web MFA, SSO and OAuth login doors record one outcome row per attempt.

Doors:
- Web ``POST /admin/mfa/challenge/verify`` (answers a password or SSO
  login's MFA challenge and creates the web session);
- ``GET /auth/sso/callback`` (SSO login: a web session, or an OAuth
  authorization code on the OAuth-authorize branch);
- ``POST /oauth/authorize`` (password login issuing an authorization code);
- ``POST /oauth/mfa/verify`` (answers an OAuth login's MFA challenge).

Each attempt records exactly one ``authentication_success`` or
``authentication_failure`` row; a step that only returns an MFA challenge
records nothing.  A refused attempt names the account only when it exists.
The async doors record off the event loop.  No password, TOTP code,
recovery code or authorization code ever reaches a row.

Front door: the real routers on a FastAPI app with the real audit request
context middleware, real UserManager, TOTPService, MFA challenge manager,
OAuthManager, web session manager and AuditLogService (bound as the audit
sink).  The external identity provider is replaced by a fake provider.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, List

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _audit_mfa_login_support import AuditStore, bound_audit_store, capture_errors
from code_indexer.server.auth.mfa_challenge import mfa_challenge_manager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)
from code_indexer.server.services.audit_events import UNKNOWN_ACCOUNT_ACTOR
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web.auth import SessionManager

_LOGIN_TYPES = ("authentication_success", "authentication_failure")
_USER = "login-user"
_MFA_USER = "login-mfa-user"
_PASSWORD = "Login-Door-Pa55word!"
# TestClient reports this as request.client.host.
_CLIENT_IP = "testclient"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


@pytest.fixture
def users(tmp_path: Path) -> UserManager:
    um = UserManager(users_file_path=str(tmp_path / "users.json"))
    um.create_user(_USER, _PASSWORD, UserRole.NORMAL_USER)
    um.create_user(_MFA_USER, _PASSWORD, UserRole.ADMIN)
    return um


@pytest.fixture
def totp(tmp_path: Path) -> Iterator[TOTPService]:
    svc = TOTPService(db_path=str(tmp_path / "mfa.db"))
    previous = mfa_routes.get_totp_service()
    mfa_routes.set_totp_service(svc)
    yield svc
    mfa_routes.set_totp_service(previous)


@pytest.fixture
def mfa_secret(totp: TOTPService) -> str:
    secret = totp.generate_secret(_MFA_USER)
    assert totp.activate_mfa(_MFA_USER, pyotp.TOTP(secret).now())
    return secret


@pytest.fixture
def sessions(monkeypatch, users) -> SessionManager:
    from code_indexer.server.auth import dependencies

    sm = SessionManager(
        "audit-login-test-signing-key", SimpleNamespace(host="127.0.0.1")
    )
    monkeypatch.setattr(web_auth, "_session_manager", sm)
    # The shared account store start-up wires (MFA completion resolves it).
    monkeypatch.setattr(dependencies, "user_manager", users)
    return sm


def _app(*routers) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    for router in routers:
        app.include_router(router)
    return app


def _next_step_code(secret: str) -> str:
    """A valid code from the NEXT time step (the current one was used to enroll)."""
    return str(pyotp.TOTP(secret).at(int(time.time()) + 30))


def _outcomes(store: AuditStore) -> List[tuple]:
    return [
        (r.action_type, r.actor, r.target_id, r.outcome)
        for r in store.rows(*_LOGIN_TYPES)
    ]


# ---------------------------------------------------------------------------
# Web MFA challenge verify
# ---------------------------------------------------------------------------


@pytest.fixture
def web_mfa(store, totp, sessions) -> TestClient:
    return TestClient(
        _app(mfa_routes.mfa_router),
        raise_server_exceptions=False,
        follow_redirects=False,
    )


def _web_challenge(username: str = _MFA_USER, client_ip: str = _CLIENT_IP) -> str:
    return str(
        mfa_challenge_manager.create_challenge(
            username=username, role="admin", client_ip=client_ip, redirect_url="/admin/"
        )
    )


class TestWebMfaChallengeDoor:
    def test_valid_totp_code_records_one_success_row(
        self, web_mfa, mfa_secret, store, caplog
    ):
        code = _next_step_code(mfa_secret)

        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={"challenge_token": _web_challenge(), "totp_code": code},
        )

        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/admin/"
        assert _outcomes(store) == [
            ("authentication_success", _MFA_USER, _MFA_USER, "success")
        ]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "password",
            "mfa": "totp",
            "flow": "web_session",
        }
        assert (row.source, row.auth_method) == ("web", "web_session")
        assert code not in store.all_raw_text()
        assert capture_errors(caplog) == []

    def test_valid_recovery_code_records_the_recovery_factor(
        self, web_mfa, totp, mfa_secret, store
    ):
        recovery = totp.generate_recovery_codes(_MFA_USER)[0]

        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={"challenge_token": _web_challenge(), "recovery_code": recovery},
        )

        assert resp.status_code == 303, resp.text
        rows = store.rows(*_LOGIN_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("authentication_success", "success")
        ]
        assert rows[0].details["mfa"] == "recovery_code"
        assert recovery not in store.all_raw_text()

    def test_wrong_code_records_one_failure_naming_the_account(
        self, web_mfa, mfa_secret, store
    ):
        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={"challenge_token": _web_challenge(), "totp_code": "000000"},
        )

        assert resp.headers["location"] == "/login?info=mfa_failed"
        assert _outcomes(store) == [
            ("authentication_failure", _MFA_USER, _MFA_USER, "failure")
        ]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "password",
            "stage": "mfa_code",
            "reason": "mfa_code_invalid",
        }
        assert row.auth_method == "none"

    def test_unknown_challenge_records_a_failure_for_an_unknown_account(
        self, web_mfa, mfa_secret, store
    ):
        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={"challenge_token": "no-such-challenge", "totp_code": "123456"},
        )

        assert resp.headers["location"] == "/login?info=mfa_expired"
        assert _outcomes(store) == [
            (
                "authentication_failure",
                UNKNOWN_ACCOUNT_ACTOR,
                UNKNOWN_ACCOUNT_ACTOR,
                "failure",
            )
        ]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "password",
            "stage": "challenge",
            "reason": "challenge_invalid_or_expired",
        }

    def test_challenge_from_another_address_records_one_failure(
        self, web_mfa, mfa_secret, store
    ):
        token = _web_challenge(client_ip="192.0.2.99")

        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={"challenge_token": token, "totp_code": _next_step_code(mfa_secret)},
        )

        assert resp.headers["location"] == "/login?info=mfa_expired"
        assert _outcomes(store) == [
            ("authentication_failure", _MFA_USER, _MFA_USER, "failure")
        ]
        assert store.rows(*_LOGIN_TYPES)[0].details["stage"] == "challenge"


# ---------------------------------------------------------------------------
# OAuth doors
# ---------------------------------------------------------------------------

_REDIRECT_URI = "https://client.example.com/callback"
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.fixture
def oauth_manager(tmp_path: Path):
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

    return OAuthManager(
        db_path=str(tmp_path / "oauth.db"), issuer="http://localhost:8000"
    )


@pytest.fixture
def oauth_client_id(oauth_manager) -> str:
    client = oauth_manager.register_client(
        client_name="Audit Test Client", redirect_uris=[_REDIRECT_URI]
    )
    return str(client["client_id"])


@pytest.fixture
def oauth(store, totp, users, oauth_manager) -> TestClient:
    from code_indexer.server.auth.oauth import routes as oauth_routes

    app = _app(oauth_routes.router)
    app.state.oauth_manager = oauth_manager
    app.dependency_overrides[oauth_routes.get_user_manager] = lambda: users
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def _oauth_challenge(
    client_id: object, username: str = _MFA_USER, client_ip: str = _CLIENT_IP
) -> str:
    return str(
        mfa_challenge_manager.create_challenge(
            username=username,
            role="admin",
            client_ip=client_ip,
            redirect_url="/oauth/authorize",
            oauth_client_id=client_id,  # type: ignore[arg-type]
            oauth_redirect_uri=_REDIRECT_URI,
            oauth_code_challenge=_CODE_CHALLENGE,
            oauth_state="client-state",
        )
    )


def _issued_code(location: str) -> str:
    return location.split("code=", 1)[1].split("&", 1)[0]


def _authorize(
    client: TestClient,
    client_id: str,
    username: str,
    password: str = _PASSWORD,
    redirect_uri: str = _REDIRECT_URI,
):
    return client.post(
        "/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": _CODE_CHALLENGE,
            "state": "client-state",
            "username": username,
            "password": password,
        },
    )


class TestOAuthAuthorizeDoor:
    def test_password_login_records_one_success_row_off_the_event_loop(
        self, oauth, oauth_client_id, store, caplog
    ):
        resp = _authorize(oauth, oauth_client_id, _USER)

        assert resp.status_code == 200, resp.text
        assert _outcomes(store) == [("authentication_success", _USER, _USER, "success")]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "password",
            "mfa": "not_enrolled",
            "flow": "oauth_code",
        }
        assert capture_errors(caplog) == []
        raw = store.all_raw_text()
        assert _PASSWORD not in raw
        assert resp.json()["code"] not in raw

    def test_wrong_password_records_one_failure_naming_the_account(
        self, oauth, oauth_client_id, store, caplog
    ):
        resp = _authorize(oauth, oauth_client_id, _USER, password="Wrong-Pa55word!")

        assert resp.status_code == 401, resp.text
        assert _outcomes(store) == [("authentication_failure", _USER, _USER, "failure")]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "password",
            "stage": "credentials",
            "reason": "bad_credentials",
        }
        assert "Wrong-Pa55word!" not in store.all_raw_text()
        assert capture_errors(caplog) == []

    def test_unknown_account_is_recorded_as_unknown(
        self, oauth, oauth_client_id, store
    ):
        resp = _authorize(oauth, oauth_client_id, "typed-into-wrong-field")

        assert resp.status_code == 401, resp.text
        assert _outcomes(store) == [
            (
                "authentication_failure",
                UNKNOWN_ACCOUNT_ACTOR,
                UNKNOWN_ACCOUNT_ACTOR,
                "failure",
            )
        ]
        assert "typed-into-wrong-field" not in store.all_raw_text()

    def test_issuance_error_records_one_failure(self, oauth, oauth_client_id, store):
        resp = _authorize(
            oauth,
            oauth_client_id,
            _USER,
            redirect_uri="https://unregistered.example.com/cb",
        )

        assert resp.status_code == 400, resp.text
        assert _outcomes(store) == [("authentication_failure", _USER, _USER, "failure")]
        assert store.rows(*_LOGIN_TYPES)[0].details["stage"] == "issuance"

    def test_mfa_challenge_step_records_nothing(
        self, oauth, oauth_client_id, mfa_secret, store
    ):
        resp = _authorize(oauth, oauth_client_id, _MFA_USER)

        assert resp.status_code == 200, resp.text
        assert "/oauth/mfa/verify" in resp.text
        assert store.rows(*_LOGIN_TYPES) == []


# ---------------------------------------------------------------------------
# SSO callback
# ---------------------------------------------------------------------------

_SSO_EMAIL = "sso-person@example.com"


class _FakeIdentityProvider:
    """Stands in for the EXTERNAL OIDC identity provider (network service)."""

    def __init__(self, *, fail_exchange: bool = False, id_token: bool = True):
        self.fail_exchange = fail_exchange
        self.id_token = id_token

    async def exchange_code_for_token(self, code, code_verifier, redirect_uri):
        if self.fail_exchange:
            raise Exception("identity provider rejected the authorization code")
        tokens = {"access_token": "idp-access-token"}
        if self.id_token:
            tokens["id_token"] = "idp-id-token"
        return tokens

    def get_user_info(self, access_token, id_token):
        from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo

        return OIDCUserInfo(subject="sub-1", email=_SSO_EMAIL, email_verified=True)


class _Sso:
    def __init__(self, client: TestClient, state_manager, oidc_manager) -> None:
        self.client = client
        self.state_manager = state_manager
        self.oidc_manager = oidc_manager

    def callback(self, state_data: dict):
        state = self.state_manager.create_state(state_data)
        return self.client.get(f"/auth/sso/callback?code=idp-code&state={state}")


@pytest.fixture
def sso(
    store, totp, users, sessions, oauth_manager, monkeypatch, tmp_path: Path
) -> _Sso:
    from code_indexer.server.auth.oidc import routes as oidc_routes
    from code_indexer.server.auth.oidc import state_manager as state_module
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.state_manager import StateManager
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    # The state store's shared path is module-level wiring another test may
    # have left pointing at its own (deleted) temp dir: pin this test's.
    monkeypatch.setattr(
        state_module, "_configured_sqlite_path", str(tmp_path / "oidc_state.db")
    )

    config = OIDCProviderConfig(
        enabled=True, issuer_url="https://idp.example.com", client_id="cidx"
    )
    manager = OIDCManager(config, None, None)
    manager.provider = _FakeIdentityProvider()

    async def _match_existing_account(user_info):
        # Account provisioning is not under test: the SSO identity maps to
        # the existing login-user account.
        return users.get_user(_USER)

    manager.match_or_create_user = _match_existing_account  # type: ignore[method-assign]
    state_manager = StateManager()
    monkeypatch.setattr(oidc_routes, "oidc_manager", manager)
    monkeypatch.setattr(oidc_routes, "state_manager", state_manager)
    app = _app(oidc_routes.router)
    app.state.oauth_manager = oauth_manager
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    return _Sso(client, state_manager, manager)


def _oauth_state(client_id: str) -> dict:
    return {
        "flow": "oauth_authorize",
        "code_verifier": "cv",
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "code_challenge": _CODE_CHALLENGE,
        "oauth_state": "client-state",
    }


_UNKNOWN_FAILURE = (
    "authentication_failure",
    UNKNOWN_ACCOUNT_ACTOR,
    UNKNOWN_ACCOUNT_ACTOR,
    "failure",
)


class TestSsoCallbackDoor:
    def test_web_session_login_records_one_success_row_off_the_event_loop(
        self, sso, store, caplog
    ):
        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 302, resp.text
        assert _outcomes(store) == [("authentication_success", _USER, _USER, "success")]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "sso",
            "mfa": "not_enrolled",
            "flow": "web_session",
        }
        assert row.auth_method == "web_session"
        assert capture_errors(caplog) == []

    def test_oauth_authorize_branch_records_one_success_row_off_the_event_loop(
        self, sso, oauth_client_id, store, caplog
    ):
        resp = sso.callback(_oauth_state(oauth_client_id))

        assert resp.status_code == 302, resp.text
        assert resp.headers["location"].startswith(_REDIRECT_URI)
        assert _outcomes(store) == [("authentication_success", _USER, _USER, "success")]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "sso",
            "mfa": "not_enrolled",
            "flow": "oauth_code",
        }
        assert row.auth_method == "oauth_token"
        assert _issued_code(resp.headers["location"]) not in store.all_raw_text()
        assert capture_errors(caplog) == []

    @pytest.mark.parametrize("flow", ["web_session", "oauth_code"])
    def test_mfa_challenge_step_records_nothing(
        self, sso, oauth_client_id, totp, store, flow
    ):
        secret = totp.generate_secret(_USER)
        assert totp.activate_mfa(_USER, pyotp.TOTP(secret).now())
        state = (
            {"code_verifier": "cv"}
            if flow == "web_session"
            else _oauth_state(oauth_client_id)
        )

        resp = sso.callback(state)

        assert resp.status_code == 200, resp.text
        assert "challenge_token" in resp.text
        assert store.rows(*_LOGIN_TYPES) == []

    def test_sso_login_with_mfa_records_exactly_one_row_across_both_steps(
        self, sso, web_mfa, totp, store
    ):
        import re

        secret = totp.generate_secret(_USER)
        assert totp.activate_mfa(_USER, pyotp.TOTP(secret).now())
        page = sso.callback({"code_verifier": "cv"})
        match = re.search(r"name='challenge_token' value='([^']+)'", page.text)
        assert match is not None, page.text

        resp = web_mfa.post(
            "/admin/mfa/challenge/verify",
            data={
                "challenge_token": match.group(1),
                "totp_code": _next_step_code(secret),
            },
        )

        assert resp.status_code == 303, resp.text
        assert _outcomes(store) == [("authentication_success", _USER, _USER, "success")]
        assert store.rows(*_LOGIN_TYPES)[0].details["flow"] == "web_session"

    def test_invalid_state_records_one_failure_for_an_unknown_account(
        self, sso, store, caplog
    ):
        resp = sso.client.get("/auth/sso/callback?code=idp-code&state=unknown-state")

        assert resp.status_code == 400, resp.text
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "sso",
            "stage": "challenge",
            "reason": "challenge_invalid_or_expired",
        }
        assert capture_errors(caplog) == []

    def test_unauthorised_identity_records_one_failure(self, sso, store):
        async def _no_account(user_info):
            return None

        sso.oidc_manager.match_or_create_user = _no_account

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 403, resp.text
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "sso",
            "stage": "credentials",
            "reason": "bad_credentials",
        }
        assert _SSO_EMAIL not in store.all_raw_text()

    def test_rejected_authorization_code_records_one_failure(self, sso, store):
        sso.oidc_manager.provider = _FakeIdentityProvider(fail_exchange=True)

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 500
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details["reason"] == "bad_credentials"

    def test_missing_id_token_records_one_failure(self, sso, store):
        sso.oidc_manager.provider = _FakeIdentityProvider(id_token=False)

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 500, resp.text
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "sso",
            "stage": "credentials",
            "reason": "server_error",
        }

    def test_unreadable_id_token_records_one_failure(self, sso, store):
        def _unparseable(access_token, id_token):
            raise ValueError("malformed ID token")

        sso.oidc_manager.provider.get_user_info = _unparseable

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 500
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details["reason"] == "bad_credentials"

    def test_account_provisioning_error_records_one_failure(self, sso, store):
        async def _provisioning_fails(user_info):
            raise RuntimeError("account store unavailable")

        sso.oidc_manager.match_or_create_user = _provisioning_fails

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 500
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details["reason"] == "server_error"

    def test_unavailable_identity_provider_records_one_failure(self, sso, store):
        async def _unreachable():
            raise ConnectionError("identity provider unreachable")

        sso.oidc_manager.ensure_provider_initialized = _unreachable

        resp = sso.callback({"code_verifier": "cv"})

        assert resp.status_code == 503, resp.text
        assert _outcomes(store) == [_UNKNOWN_FAILURE]
        assert store.rows(*_LOGIN_TYPES)[0].details["reason"] == "server_error"


class TestOAuthMfaVerifyDoor:
    def test_valid_code_records_one_success_row(
        self, oauth, oauth_client_id, mfa_secret, store
    ):
        code = _next_step_code(mfa_secret)

        resp = oauth.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": _oauth_challenge(oauth_client_id),
                "totp_code": code,
            },
        )

        assert resp.status_code == 302, resp.text
        assert _outcomes(store) == [
            ("authentication_success", _MFA_USER, _MFA_USER, "success")
        ]
        row = store.rows(*_LOGIN_TYPES)[0]
        assert row.details == {
            "method": "password",
            "mfa": "totp",
            "flow": "oauth_code",
        }
        assert row.auth_method == "oauth_token"
        raw = store.all_raw_text()
        assert code not in raw
        assert _issued_code(resp.headers["location"]) not in raw

    def test_wrong_code_records_one_failure(
        self, oauth, oauth_client_id, mfa_secret, store
    ):
        resp = oauth.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": _oauth_challenge(oauth_client_id),
                "totp_code": "000000",
            },
        )

        assert resp.status_code == 401, resp.text
        assert _outcomes(store) == [
            ("authentication_failure", _MFA_USER, _MFA_USER, "failure")
        ]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "password",
            "stage": "mfa_code",
            "reason": "mfa_code_invalid",
        }

    def test_unknown_challenge_records_a_failure_for_an_unknown_account(
        self, oauth, mfa_secret, store
    ):
        resp = oauth.post(
            "/oauth/mfa/verify",
            data={"challenge_token": "no-such-challenge", "totp_code": "123456"},
        )

        assert resp.status_code == 400, resp.text
        assert _outcomes(store) == [
            (
                "authentication_failure",
                UNKNOWN_ACCOUNT_ACTOR,
                UNKNOWN_ACCOUNT_ACTOR,
                "failure",
            )
        ]
        assert store.rows(*_LOGIN_TYPES)[0].details["stage"] == "challenge"

    @pytest.mark.parametrize("mismatch", ["address", "not_oauth"])
    def test_unusable_challenge_records_one_failure_naming_the_account(
        self, oauth, oauth_client_id, mfa_secret, store, mismatch
    ):
        if mismatch == "address":
            token = _oauth_challenge(oauth_client_id, client_ip="192.0.2.99")
        else:
            token = _web_challenge()  # no OAuth context

        resp = oauth.post(
            "/oauth/mfa/verify",
            data={"challenge_token": token, "totp_code": _next_step_code(mfa_secret)},
        )

        assert resp.status_code == 400, resp.text
        assert _outcomes(store) == [
            ("authentication_failure", _MFA_USER, _MFA_USER, "failure")
        ]
        assert store.rows(*_LOGIN_TYPES)[0].details == {
            "method": "password",
            "stage": "challenge",
            "reason": "challenge_invalid_or_expired",
        }
