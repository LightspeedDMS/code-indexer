"""API documentation requires an authenticated session or token.

GET /docs (Swagger UI), GET /redoc (ReDoc) and GET /openapi.json accept
either a valid Web UI session cookie or a valid bearer token. Without one,
the HTML pages redirect to the login page and the JSON document answers 401.

Drives the real app built by create_app() through TestClient with real
logins (form login for the session cookie, /auth/login for the token).
"""

import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app

_ADMIN_USER = "admin"
_ADMIN_PASS = "admin"

_HTML_DOC_PATHS = ("/docs", "/redoc")


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def anon_client(app) -> TestClient:
    return TestClient(app)


def _bearer_token(app) -> str:
    login_client = TestClient(app)
    resp = login_client.post(
        "/auth/login", json={"username": _ADMIN_USER, "password": _ADMIN_PASS}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    assert isinstance(token, str) and token
    return token


@pytest.fixture
def bearer_client(app) -> TestClient:
    """Client carrying ONLY an Authorization header (no cookies at all)."""
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {_bearer_token(app)}"
    return client


@pytest.fixture
def session_client(app) -> TestClient:
    """Client carrying ONLY the Web UI session cookie from a form login."""
    client = TestClient(app)
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "csrf_token not found on login page"
    resp = client.post(
        "/login",
        data={
            "username": _ADMIN_USER,
            "password": _ADMIN_PASS,
            "csrf_token": match.group(1),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.status_code
    assert "session" in resp.cookies, "form login set no session cookie"
    client.cookies.clear()
    client.cookies.set("session", resp.cookies["session"])
    assert "Authorization" not in client.headers
    return client


class TestUnauthenticatedRefused:
    @pytest.mark.parametrize("path", _HTML_DOC_PATHS)
    def test_html_doc_page_redirects_to_login(self, anon_client, path):
        resp = anon_client.get(path, follow_redirects=False)

        assert resp.status_code == 303, resp.status_code
        location = resp.headers["location"]
        assert location.startswith("/login?redirect_to=")
        assert unquote(location.split("redirect_to=", 1)[1]) == path
        assert "swagger" not in resp.text.lower()
        assert "redoc" not in resp.text.lower()

    def test_redirect_preserves_query_string(self, anon_client):
        resp = anon_client.get("/docs?deepLinking=true", follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert unquote(location.split("redirect_to=", 1)[1]) == (
            "/docs?deepLinking=true"
        )

    def test_openapi_json_returns_401(self, anon_client):
        resp = anon_client.get("/openapi.json", follow_redirects=False)

        assert resp.status_code == 401
        assert "paths" not in resp.text

    def test_openapi_json_invalid_bearer_returns_401(self, anon_client):
        resp = anon_client.get(
            "/openapi.json",
            headers={"Authorization": "Bearer not.a.valid.token"},
            follow_redirects=False,
        )

        assert resp.status_code == 401
        assert "paths" not in resp.text

    def test_invalid_session_cookie_is_refused(self, anon_client):
        anon_client.cookies.set("session", "forged-cookie-value")

        resp = anon_client.get("/openapi.json", follow_redirects=False)

        assert resp.status_code == 401

    def test_builtin_oauth2_redirect_page_not_served(self, anon_client):
        resp = anon_client.get("/docs/oauth2-redirect", follow_redirects=False)

        assert resp.status_code == 404


def _assert_swagger(resp) -> None:
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-type"].startswith("text/html")
    assert "swagger-ui" in resp.text.lower()
    # Same-origin fetch of the schema: the browser sends the session cookie.
    assert "'/openapi.json'" in resp.text or '"/openapi.json"' in resp.text


def _assert_redoc(resp) -> None:
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-type"].startswith("text/html")
    assert "redoc" in resp.text.lower()
    assert "/openapi.json" in resp.text


def _assert_openapi(resp) -> None:
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["content-type"].startswith("application/json")
    spec = resp.json()
    assert spec["openapi"].startswith("3.")
    paths = spec["paths"]
    assert "/auth/login" in paths
    assert "/api/query" in paths
    # The documentation routes themselves are not part of the API schema.
    for doc_path in ("/docs", "/redoc", "/openapi.json"):
        assert doc_path not in paths


class TestBearerTokenGrantsAccess:
    def test_swagger_ui(self, bearer_client):
        _assert_swagger(bearer_client.get("/docs", follow_redirects=False))

    def test_redoc(self, bearer_client):
        _assert_redoc(bearer_client.get("/redoc", follow_redirects=False))

    def test_openapi_json(self, bearer_client):
        _assert_openapi(bearer_client.get("/openapi.json", follow_redirects=False))


def _signed_session_cookie(username: str) -> str:
    """A correctly signed Web UI session cookie for ``username``."""
    from fastapi import Response

    from code_indexer.server.web.auth import SESSION_COOKIE_NAME, get_session_manager

    response = Response()
    get_session_manager().create_session(response, username, "normal_user")
    set_cookie = response.headers["set-cookie"]
    prefix = f"{SESSION_COOKIE_NAME}="
    assert set_cookie.startswith(prefix), set_cookie
    return set_cookie[len(prefix) :].split(";", 1)[0]


class TestCanonicalCookieOrTokenResolution:
    """The docs use the app's shared cookie-or-token resolver."""

    @pytest.mark.parametrize("path", ("/openapi.json", "/docs"))
    def test_valid_bearer_with_garbage_session_cookie_succeeds(self, app, path):
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {_bearer_token(app)}"
        client.cookies.set("session", "garbage-not-a-signed-session")

        resp = client.get(path, follow_redirects=False)

        assert resp.status_code == 200, resp.status_code

    def test_signed_session_for_missing_user_redirects_html(self, app):
        client = TestClient(app)
        client.cookies.set("session", _signed_session_cookie("no-such-user-a5"))

        resp = client.get("/docs", follow_redirects=False)

        assert resp.status_code == 303, resp.status_code
        assert "/login?redirect_to=" in resp.headers["location"]

    def test_signed_session_for_missing_user_json_is_401(self, app):
        client = TestClient(app)
        client.cookies.set("session", _signed_session_cookie("no-such-user-a5"))

        resp = client.get("/openapi.json", follow_redirects=False)

        assert resp.status_code == 401, resp.status_code


class TestReverseProxyRootPath:
    """The pages load the schema under the proxy prefix, as FastAPI's do."""

    @pytest.mark.parametrize("path", _HTML_DOC_PATHS)
    def test_schema_url_honours_root_path(self, app, path):
        client = TestClient(app, root_path="/proxied")
        client.headers["Authorization"] = f"Bearer {_bearer_token(app)}"

        resp = client.get(path, follow_redirects=False)

        assert resp.status_code == 200, resp.status_code
        assert "/proxied/openapi.json" in resp.text

    def test_schema_servers_names_root_path(self, app):
        """Swagger "Try it out" must target the proxy prefix."""
        client = TestClient(app, root_path="/proxied")
        client.headers["Authorization"] = f"Bearer {_bearer_token(app)}"

        resp = client.get("/openapi.json")

        assert resp.status_code == 200, resp.status_code
        assert resp.json()["servers"] == [{"url": "/proxied"}]

    def test_root_path_servers_entry_never_mutates_cached_schema(self, app):
        """One app, two requests: the prefixed request's servers entry must not
        leak into the app's cached schema or into an unprefixed request."""
        token = _bearer_token(app)
        auth = {"Authorization": f"Bearer {token}"}

        proxied = TestClient(app, root_path="/proxied").get(
            "/openapi.json", headers=auth
        )
        assert proxied.status_code == 200, proxied.status_code
        assert proxied.json()["servers"] == [{"url": "/proxied"}]

        direct = TestClient(app).get("/openapi.json", headers=auth)
        assert direct.status_code == 200, direct.status_code
        assert "servers" not in direct.json()

        assert "servers" not in (app.openapi_schema or {})

    def test_schema_has_no_servers_without_root_path(self, bearer_client):
        resp = bearer_client.get("/openapi.json")

        assert resp.status_code == 200, resp.status_code
        assert "servers" not in resp.json()


def _scope_request(path: str, root_path: str, query: bytes = b""):
    """A real Starlette Request over a minimal HTTP scope."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": root_path,
            "query_string": query,
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


class TestLoginRedirectUnderRootPath:
    """The login redirect stays inside the reverse-proxy prefix."""

    def test_anonymous_redirect_keeps_proxy_prefix(self, app):
        client = TestClient(app, root_path="/proxied")

        resp = client.get("/docs?x=1", follow_redirects=False)

        assert resp.status_code == 303, resp.status_code
        location = resp.headers["location"]
        assert location.startswith("/proxied/login?redirect_to=")
        assert unquote(location.split("redirect_to=", 1)[1]) == "/proxied/docs?x=1"

    def test_prefix_not_doubled_when_scope_path_carries_it(self):
        from code_indexer.server.routers.api_docs import _login_location

        request = _scope_request("/proxied/redoc", "/proxied")

        location = _login_location(request)

        assert location.startswith("/proxied/login?redirect_to=")
        assert unquote(location.split("redirect_to=", 1)[1]) == "/proxied/redoc"

    def test_no_root_path_uses_bare_login(self):
        from code_indexer.server.routers.api_docs import _login_location

        assert _login_location(_scope_request("/docs", "")) == (
            "/login?redirect_to=/docs"
        )


class TestSessionCookieGrantsAccess:
    def test_swagger_ui(self, session_client):
        _assert_swagger(session_client.get("/docs", follow_redirects=False))

    def test_redoc(self, session_client):
        _assert_redoc(session_client.get("/redoc", follow_redirects=False))

    def test_openapi_json(self, session_client):
        _assert_openapi(session_client.get("/openapi.json", follow_redirects=False))
