"""
Unit tests for the toggle_cidx_meta_backup E2E helper (Story #926).

Tests cover:
  - _extract_csrf_token_from_html: token extraction from rendered HTML.
  - toggle_cidx_meta_backup guard-rail validation (None / empty inputs).
  - 4-request sequence: GET /login -> POST /login -> GET /admin/config ->
    POST /admin/config/cidx_meta_backup.
  - CSRF token from each form included in the corresponding POST.
  - Session cookie set by POST /login carried on subsequent requests.

Shared fixtures:
  - _bare_client: yields an httpx.Client for guard-rail validation tests.
  - recorded: runs toggle_cidx_meta_backup and returns the recording transport.
"""

from __future__ import annotations

from typing import Callable, Generator, List
from urllib.parse import unquote_plus

import httpx
import pytest

from tests.e2e.helpers import (
    _extract_csrf_token_from_html,
    toggle_cidx_meta_backup,
)

# ---------------------------------------------------------------------------
# Stub HTML and token constants
# ---------------------------------------------------------------------------

_LOGIN_CSRF = "login-csrf-token-abc123"
_CONFIG_CSRF = "config-csrf-token-def456"

_LOGIN_PAGE_HTML = (
    "<html><body>"
    '<form method="post" action="/login">'
    f'<input type="hidden" name="csrf_token" value="{_LOGIN_CSRF}" />'
    '<input type="text" name="username" />'
    '<input type="password" name="password" />'
    "</form></body></html>"
)

_CONFIG_PAGE_HTML = (
    "<html><body>"
    '<form method="post" action="/admin/config/cidx_meta_backup">'
    f'<input type="hidden" name="csrf_token" value="{_CONFIG_CSRF}" />'
    '<select name="enabled"><option value="true">Yes</option></select>'
    '<input type="text" name="remote_url" />'
    "</form></body></html>"
)

_CONFIG_SAVE_HTML = (
    '<html><body><p class="success">Configuration saved.</p></body></html>'
)

_SESSION_COOKIE_NAME = "cidx_session"
_CSRF_COOKIE_NAME = "cidx_csrf"


# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.MockTransport):
    """httpx transport that records requests and routes to a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: List[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _make_transport() -> _RecordingTransport:
    """Build a mock transport simulating the 4-step web-form flow.

    GET /login          -> 200, sets CSRF cookie, HTML with login CSRF token.
    POST /login         -> 303 redirect, sets session cookie.
    GET /admin/config   -> 200, HTML with config CSRF token.
    POST /admin/config/ -> 200, save confirmation HTML.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path

        if method == "GET" and path == "/login":
            return httpx.Response(
                200,
                text=_LOGIN_PAGE_HTML,
                headers={
                    "set-cookie": (
                        f"{_CSRF_COOKIE_NAME}={_LOGIN_CSRF}; Path=/; SameSite=Lax"
                    ),
                    "content-type": "text/html",
                },
            )

        if method == "POST" and path == "/login":
            return httpx.Response(
                303,
                headers={
                    "location": "/admin/",
                    "set-cookie": (
                        f"{_SESSION_COOKIE_NAME}=mock-session-value; "
                        "Path=/; HttpOnly; SameSite=Lax"
                    ),
                    "content-type": "text/html",
                },
                text="",
            )

        if method == "GET" and path == "/admin/config":
            return httpx.Response(
                200,
                text=_CONFIG_PAGE_HTML,
                headers={"content-type": "text/html"},
            )

        if method == "POST" and path.startswith("/admin/config/"):
            return httpx.Response(
                200,
                text=_CONFIG_SAVE_HTML,
                headers={"content-type": "text/html"},
            )

        raise AssertionError(f"Unexpected request: {method} {path}")

    return _RecordingTransport(_handler)


def _decode_form(body: bytes) -> dict:
    """Parse application/x-www-form-urlencoded body into a str->str dict."""
    result = {}
    for pair in body.decode().split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[unquote_plus(k)] = unquote_plus(v)
    return result


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _bare_client() -> Generator[httpx.Client, None, None]:
    """Yield an httpx.Client backed by a fresh mock transport.

    Used by guard-rail validation tests so transport setup is not repeated.
    """
    transport = _make_transport()
    with httpx.Client(base_url="http://testserver", transport=transport) as client:
        yield client


@pytest.fixture()
def recorded(request) -> _RecordingTransport:
    """Run toggle_cidx_meta_backup and return the recording transport.

    Parametrize via indirect to vary enabled/remote_url:
        pytest.param({"enabled": False, "remote_url": ""}, indirect=["recorded"])
    Defaults: enabled=True, remote_url="file:///tmp/backup.git".
    """
    params = getattr(request, "param", {})
    enabled = params.get("enabled", True)
    remote_url = params.get("remote_url", "file:///tmp/backup.git")

    transport = _make_transport()
    with httpx.Client(
        base_url="http://testserver",
        transport=transport,
        follow_redirects=False,
        cookies={},
    ) as client:
        toggle_cidx_meta_backup(
            client,
            admin_user="admin",
            admin_pass="admin",
            enabled=enabled,
            remote_url=remote_url,
        )
    return transport


# ---------------------------------------------------------------------------
# Tests: _extract_csrf_token_from_html
# ---------------------------------------------------------------------------


class TestExtractCsrfTokenFromHtml:
    """Unit tests for the CSRF extraction helper used internally by toggle_cidx_meta_backup."""

    def test_extracts_token_from_double_quoted_value(self):
        html = '<input name="csrf_token" value="tok123" />'
        assert _extract_csrf_token_from_html(html) == "tok123"

    def test_extracts_token_from_single_quoted_value(self):
        html = "<input name='csrf_token' value='tok456' />"
        assert _extract_csrf_token_from_html(html) == "tok456"

    def test_raises_value_error_when_not_found(self):
        html = "<html><body>No CSRF here</body></html>"
        with pytest.raises(ValueError, match="CSRF token not found"):
            _extract_csrf_token_from_html(html)

    def test_extracts_from_login_page_stub(self):
        assert _extract_csrf_token_from_html(_LOGIN_PAGE_HTML) == _LOGIN_CSRF

    def test_extracts_from_config_page_stub(self):
        assert _extract_csrf_token_from_html(_CONFIG_PAGE_HTML) == _CONFIG_CSRF


# ---------------------------------------------------------------------------
# Tests: guard-rail validation
# ---------------------------------------------------------------------------


class TestToggleCidxMetaBackupValidation:
    """Guard-rail validation: helper rejects None/empty inputs before any HTTP call."""

    def test_raises_when_client_is_none(self):
        with pytest.raises(ValueError, match="client must not be None"):
            toggle_cidx_meta_backup(
                None,  # type: ignore[arg-type]  # intentional: guard-rail test for None-rejection in validation path
                admin_user="admin",
                admin_pass="pass",
                enabled=True,
                remote_url="file:///tmp/r.git",
            )

    def test_raises_when_admin_user_is_empty(self, _bare_client: httpx.Client):
        with pytest.raises(ValueError, match="admin_user must be non-empty"):
            toggle_cidx_meta_backup(
                _bare_client,
                admin_user="",
                admin_pass="pass",
                enabled=True,
                remote_url="file:///tmp/r.git",
            )

    def test_raises_when_admin_pass_is_empty(self, _bare_client: httpx.Client):
        with pytest.raises(ValueError, match="admin_pass must be non-empty"):
            toggle_cidx_meta_backup(
                _bare_client,
                admin_user="admin",
                admin_pass="",
                enabled=True,
                remote_url="file:///tmp/r.git",
            )

    def test_raises_when_remote_url_is_none(self, _bare_client: httpx.Client):
        with pytest.raises(ValueError, match="remote_url must not be None"):
            toggle_cidx_meta_backup(
                _bare_client,
                admin_user="admin",
                admin_pass="pass",
                enabled=True,
                remote_url=None,  # type: ignore[arg-type]  # intentional: guard-rail test for None-rejection in validation path
            )


# ---------------------------------------------------------------------------
# Tests: 4-request sequence
# ---------------------------------------------------------------------------


class TestToggleCidxMetaBackupRequestSequence:
    """Verify the helper makes exactly 4 requests in the correct order."""

    def test_makes_exactly_four_requests(self, recorded: _RecordingTransport):
        assert len(recorded.requests) == 4, (
            f"Expected 4 requests, got {len(recorded.requests)}: "
            + ", ".join(f"{r.method} {r.url.path}" for r in recorded.requests)
        )

    def test_first_request_is_get_login(self, recorded: _RecordingTransport):
        req = recorded.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/login"

    def test_second_request_is_post_login(self, recorded: _RecordingTransport):
        req = recorded.requests[1]
        assert req.method == "POST"
        assert req.url.path == "/login"

    def test_third_request_is_get_admin_config(self, recorded: _RecordingTransport):
        req = recorded.requests[2]
        assert req.method == "GET"
        assert req.url.path == "/admin/config"

    def test_fourth_request_is_post_cidx_meta_backup(
        self, recorded: _RecordingTransport
    ):
        req = recorded.requests[3]
        assert req.method == "POST"
        assert req.url.path == "/admin/config/cidx_meta_backup"


# ---------------------------------------------------------------------------
# Tests: CSRF token propagation
# ---------------------------------------------------------------------------


class TestToggleCidxMetaBackupCsrfPropagation:
    """Verify CSRF tokens from each form are included in the corresponding POSTs."""

    def test_login_post_includes_login_csrf_token(self, recorded: _RecordingTransport):
        form = _decode_form(recorded.requests[1].content)
        assert form.get("csrf_token") == _LOGIN_CSRF, (
            f"login POST csrf_token mismatch: {form}"
        )

    def test_login_post_includes_credentials(self, recorded: _RecordingTransport):
        form = _decode_form(recorded.requests[1].content)
        assert form.get("username") == "admin"
        assert form.get("password") == "admin"

    def test_config_post_includes_config_csrf_token(
        self, recorded: _RecordingTransport
    ):
        form = _decode_form(recorded.requests[3].content)
        assert form.get("csrf_token") == _CONFIG_CSRF, (
            f"config POST csrf_token mismatch: {form}"
        )

    @pytest.mark.parametrize(
        "recorded,expected",
        [
            ({"enabled": True, "remote_url": "file:///r.git"}, "true"),
            ({"enabled": False, "remote_url": ""}, "false"),
        ],
        indirect=["recorded"],
    )
    def test_config_post_enabled_field(
        self, recorded: _RecordingTransport, expected: str
    ):
        form = _decode_form(recorded.requests[3].content)
        assert form.get("enabled") == expected, (
            f"enabled field mismatch (expected {expected!r}): {form}"
        )

    def test_config_post_includes_remote_url(self, recorded: _RecordingTransport):
        form = _decode_form(recorded.requests[3].content)
        assert form.get("remote_url") == "file:///tmp/backup.git", (
            f"remote_url field mismatch: {form}"
        )


# ---------------------------------------------------------------------------
# Tests: session cookie propagation
# ---------------------------------------------------------------------------


class TestToggleCidxMetaBackupSessionCookie:
    """Verify the session cookie from POST /login is carried on later requests."""

    def test_session_cookie_carried_on_get_admin_config(
        self, recorded: _RecordingTransport
    ):
        cookie_header = recorded.requests[2].headers.get("cookie", "")
        assert _SESSION_COOKIE_NAME in cookie_header, (
            f"Session cookie missing from GET /admin/config. "
            f"Cookie header: {cookie_header!r}"
        )

    def test_session_cookie_carried_on_post_cidx_meta_backup(
        self, recorded: _RecordingTransport
    ):
        cookie_header = recorded.requests[3].headers.get("cookie", "")
        assert _SESSION_COOKIE_NAME in cookie_header, (
            f"Session cookie missing from POST /admin/config/cidx_meta_backup. "
            f"Cookie header: {cookie_header!r}"
        )
