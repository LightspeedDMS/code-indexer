"""The authorization endpoint redirects only to, and renders consent only for,
a redirect_uri registered for the client (RFC 6749 section 4.1.2.1); the
consent page names the redirect host; the consent POST requires the Web UI
CSRF token; refresh tokens are bound to their client.

Front door: the real app (``create_app``) over an isolated server home,
driven through ``/oauth/register``, ``/oauth/authorize``,
``/oauth/authorize/consent`` and ``/oauth/token``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
import uuid
from http.cookies import SimpleCookie
from typing import Dict, Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import auth as web_auth
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-OAuth-Consent-Passw0rd!"
REDIRECT_URI = "https://example.com/callback"
UNREGISTERED_URI = "https://unregistered.example.net/callback"
VERIFIER = "example-pkce-code-verifier-0123456789-abcdefghij"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home, with a signed-in member's
    Web session cookie on the client."""
    root = tmp_path_factory.mktemp("oauth-redirect-app")
    with isolated_app(root) as app:
        accounts: UserManager = app.state.user_manager
        name = f"member-{uuid.uuid4().hex[:8]}"
        accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
        response = Response()
        web_auth.get_session_manager().create_session(response, name, "normal_user")
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        test_client = TestClient(app, follow_redirects=False)
        test_client.cookies.set(
            web_auth.SESSION_COOKIE_NAME, cookie[web_auth.SESSION_COOKIE_NAME].value
        )
        yield test_client


def test_authorize_get_refuses_unregistered_redirect_uri(client: TestClient) -> None:
    registered = client.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert registered.status_code == 201, registered.text

    response = client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": UNREGISTERED_URI,
            "code_challenge": CHALLENGE,
            "response_type": "code",
            "state": "example-state",
        },
    )

    assert response.status_code == 400, response.text
    assert "location" not in response.headers
    assert "/oauth/authorize/consent" not in response.text
    assert "unregistered.example.net" not in response.text


def test_consent_post_without_csrf_token_is_refused(client: TestClient) -> None:
    registered = client.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert registered.status_code == 201, registered.text

    response = client.post(
        "/oauth/authorize/consent",
        data={
            "client_id": registered.json()["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "response_type": "code",
            "consent": "allow",
        },
    )

    assert response.status_code == 403, response.text
    assert "location" not in response.headers


def _hidden_fields(page: str) -> Dict[str, str]:
    """The consent form's hidden inputs, as a browser would submit them."""
    pattern = r'<input type="hidden" name="([^"]+)" value="([^"]*)"'
    return {name: html.unescape(value) for name, value in re.findall(pattern, page)}


def test_mcp_client_flow_register_authorize_consent_token_refresh(
    client: TestClient,
) -> None:
    """A self-registering MCP client (loopback callback, PKCE) completes
    register -> authorize -> consent -> code -> token -> refresh."""
    callback = "http://localhost:33418/callback"
    registered = client.post(
        "/oauth/register",
        json={"client_name": "Example MCP Client", "redirect_uris": [callback]},
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    page = client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": callback,
            "code_challenge": challenge,
            "response_type": "code",
            "state": "example-state",
        },
    )
    assert page.status_code == 200, page.text
    fields = _hidden_fields(page.text)
    assert fields["csrf_token"]

    consent = client.post(
        "/oauth/authorize/consent", data={**fields, "consent": "allow"}
    )
    assert consent.status_code == 302, consent.text
    location = urlsplit(consent.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == callback
    query = parse_qs(location.query)
    assert query["state"] == ["example-state"]

    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert token.status_code == 200, token.text

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.json()["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != token.json()["access_token"]


def _consent_fields(client: TestClient, callback: str = REDIRECT_URI) -> Dict[str, str]:
    """Register a client for *callback*, open its consent page and return the
    form's hidden fields (CSRF token included)."""
    registered = client.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [callback]},
    )
    assert registered.status_code == 201, registered.text
    page = client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": callback,
            "code_challenge": CHALLENGE,
            "response_type": "code",
            "state": "example-state",
        },
    )
    assert page.status_code == 200, page.text
    return _hidden_fields(page.text)


def test_deny_redirects_to_the_registered_uri_with_access_denied(
    client: TestClient,
) -> None:
    fields = _consent_fields(client)

    response = client.post(
        "/oauth/authorize/consent", data={**fields, "consent": "deny"}
    )

    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT_URI
    query = parse_qs(location.query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["example-state"]
    assert "code" not in query


def test_consent_page_names_the_redirect_host(client: TestClient) -> None:
    callback = "https://trusted.example.com@consent-host.example.org:8443/cb"
    registered = client.post(
        "/oauth/register",
        json={"client_name": "<b>Example</b> Client", "redirect_uris": [callback]},
    )
    assert registered.status_code == 201, registered.text

    page = client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": callback,
            "code_challenge": CHALLENGE,
            "response_type": "code",
        },
    )

    assert page.status_code == 200, page.text
    shown = re.search(r'id="redirect-host"[^>]*>([^<]*)<', page.text)
    assert shown is not None, page.text
    assert shown.group(1).strip() == "consent-host.example.org:8443"
    assert "&lt;b&gt;Example&lt;/b&gt; Client" in page.text
    assert "<b>Example</b>" not in page.text
    assert "window.location" not in page.text


@pytest.mark.parametrize("consent", ["allow", "deny"])
def test_consent_post_with_unregistered_redirect_uri_never_redirects(
    client: TestClient, consent: str
) -> None:
    fields = _consent_fields(client)
    fields["redirect_uri"] = UNREGISTERED_URI

    response = client.post(
        "/oauth/authorize/consent", data={**fields, "consent": consent}
    )

    assert response.status_code == 400, response.text
    assert "location" not in response.headers
    assert "unregistered.example.net" not in response.text


def test_authorize_get_without_session_refuses_unregistered_redirect_uri(
    client: TestClient,
) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)
    registered = anonymous.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert registered.status_code == 201, registered.text
    params = {
        "client_id": registered.json()["client_id"],
        "code_challenge": CHALLENGE,
        "response_type": "code",
    }

    refused = anonymous.get(
        "/oauth/authorize", params={**params, "redirect_uri": UNREGISTERED_URI}
    )
    assert refused.status_code == 400, refused.text
    assert "location" not in refused.headers
    assert "unregistered.example.net" not in refused.text

    to_login = anonymous.get(
        "/oauth/authorize", params={**params, "redirect_uri": REDIRECT_URI}
    )
    assert to_login.status_code == 303, to_login.text
    assert to_login.headers["location"].startswith("/login")


def test_refresh_token_is_refused_for_another_client_at_the_token_endpoint(
    client: TestClient,
) -> None:
    verifier = VERIFIER
    fields = _consent_fields(client)
    consent = client.post(
        "/oauth/authorize/consent", data={**fields, "consent": "allow"}
    )
    assert consent.status_code == 302, consent.text
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": fields["client_id"],
        },
    )
    assert token.status_code == 200, token.text
    other = client.post(
        "/oauth/register",
        json={"client_name": "other-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert other.status_code == 201, other.text

    def refresh(client_id: str):  # type: ignore[no-untyped-def]
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.json()["refresh_token"],
                "client_id": client_id,
            },
        )

    refused = refresh(other.json()["client_id"])
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == "invalid_grant"
    assert "access_token" not in refused.json()

    assert refresh(fields["client_id"]).status_code == 200


@pytest.mark.parametrize("consent", ["allow", "deny"])
def test_consent_post_without_session_refuses_unregistered_redirect_uri(
    client: TestClient, consent: str
) -> None:
    """With no Web session the consent POST still refuses an unregistered
    redirect_uri with the error page, never a login redirect carrying it."""
    anonymous = TestClient(client.app, follow_redirects=False)
    login_page = anonymous.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert csrf is not None, login_page.text
    registered = anonymous.post(
        "/oauth/register",
        json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
    )
    assert registered.status_code == 201, registered.text

    response = anonymous.post(
        "/oauth/authorize/consent",
        data={
            "client_id": registered.json()["client_id"],
            "redirect_uri": UNREGISTERED_URI,
            "code_challenge": CHALLENGE,
            "response_type": "code",
            "consent": consent,
            "csrf_token": csrf.group(1),
        },
    )

    assert response.status_code == 400, response.text
    assert "location" not in response.headers
    assert "unregistered.example.net" not in response.text
