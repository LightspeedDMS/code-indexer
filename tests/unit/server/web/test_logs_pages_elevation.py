"""The Web log pages require TOTP elevation, matching their MCP twin
``admin_logs_query`` (``@require_mcp_elevation()``).

Server logs carry operational detail (e.g. SSH key names at INFO), so an
admin session alone must not read them.  Front doors, on the real app
(``create_app`` via ``isolated_app``, never ~/.cidx-server), with a Web
session cookie (the elevation key is the session cookie value):

- ``GET /admin/logs`` -- the page: with enforcement on and no window it
  redirects to ``/admin/elevate?next=/admin/logs``;
- ``GET /admin/partials/logs-list`` -- the HTMX partial, and
- ``GET /admin/logs/export`` -- the download: both answer 403
  ``elevation_required`` (``require_elevation()``), which the shared
  elevation interceptor turns into the TOTP modal.

With enforcement off all three serve the logs as before.  Log records are
real rows written by the real ``SQLiteLogHandler`` into a per-test logs.db
that the app reads through ``app.state.log_db_path``.  Elevation windows
live in a real ``ElevatedSessionManager`` on per-test files; only the
elevation-enforcement switch is patched.
"""

from __future__ import annotations

import logging
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-Logs-Passw0rd!"
LOGS_PAGE = "/admin/logs"
LOGS_PARTIAL = "/admin/partials/logs-list"
LOGS_EXPORT_JSON = "/admin/logs/export?format=json"
LOGS_EXPORT_CSV = "/admin/logs/export?format=csv"
_GATED_READS = (LOGS_PARTIAL, LOGS_EXPORT_JSON, LOGS_EXPORT_CSV)
_ALL_ROUTES = (LOGS_PAGE,) + _GATED_READS


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("logs-elevation-app")) as app:
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def marker(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real log row carrying a unique marker, in the logs.db the app reads."""
    text = f"example-key-name-{uuid.uuid4().hex[:12]}"
    db_path = tmp_path / "logs.db"
    handler = SQLiteLogHandler(db_path=db_path)
    try:
        handler.emit(
            logging.LogRecord(
                name="example.ssh_keys",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=f"Registered SSH key {text}",
                args=(),
                exc_info=None,
            )
        )
        handler.flush()
    finally:
        handler.close()
    state = client.app.state  # type: ignore[attr-defined]
    monkeypatch.setattr(state, "log_db_path", db_path, raising=False)
    monkeypatch.setattr(state, "logs_backend", None, raising=False)
    return text


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


@pytest.fixture
def esm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ElevatedSessionManager:
    """A real elevation-window store on this test's files, at every read point."""
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(dependencies, "elevated_session_manager", manager)
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", manager)
    return manager


@pytest.fixture
def totp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TOTPService:
    service = TOTPService(db_path=str(tmp_path / "mfa.db"))
    monkeypatch.setattr(mfa_routes, "_totp_service", service)
    return service


def _admin(accounts: UserManager, totp: TOTPService) -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    return name


def _member(accounts: UserManager) -> str:
    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    return name


def _session_cookie(username: str, role: UserRole) -> str:
    response = Response()
    web_auth.get_session_manager().create_session(response, username, role.value)
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


def _get(client: TestClient, cookie: str, url: str):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    try:
        return client.get(url)
    finally:
        client.cookies.clear()


# ---------------------------------------------------------------------------
# No elevation window: no log content
# ---------------------------------------------------------------------------


def test_logs_page_without_elevation_sends_admin_to_elevate(
    client, accounts, esm, totp, marker
) -> None:
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(True):
        response = _get(client, cookie, LOGS_PAGE)

    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"] == "/admin/elevate?next=%2Fadmin%2Flogs"
    assert marker not in response.text


def _elevate_next(response) -> str:  # type: ignore[no-untyped-def]
    """The decoded ``next`` of a redirect to the elevation page."""
    location = urlsplit(response.headers["location"])
    assert location.path == "/admin/elevate", response.headers["location"]
    next_value: str = parse_qs(location.query)["next"][0]
    return next_value


def test_logs_page_elevate_redirect_keeps_filters(
    client, accounts, esm, totp, marker
) -> None:
    """After elevating, the admin returns to the same filtered view."""
    from code_indexer.server.web.elevation_web_routes import _sanitize_next

    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(True):
        response = _get(client, cookie, f"{LOGS_PAGE}?level=ERROR&search=x")

    assert response.status_code == 303, response.text[:300]
    next_value = _elevate_next(response)
    assert next_value == f"{LOGS_PAGE}?level=ERROR&search=x"
    assert _sanitize_next(next_value) == next_value
    assert marker not in response.text


def test_logs_page_elevate_redirect_never_leaves_the_site(
    client, accounts, esm, totp, marker
) -> None:
    """A query carrying a URL never turns ``next`` into an off-site target:
    it starts with the page's own path, and the elevate page bounds it."""
    from code_indexer.server.web.elevation_web_routes import _sanitize_next

    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(True):
        response = _get(client, cookie, f"{LOGS_PAGE}?search=//example.com/x")

    assert response.status_code == 303, response.text[:300]
    next_value = _elevate_next(response)
    assert next_value.startswith(f"{LOGS_PAGE}?")
    assert _sanitize_next(next_value).startswith("/")
    assert not _sanitize_next(next_value).startswith("//")


@pytest.mark.parametrize("url", _GATED_READS)
def test_log_reads_without_elevation_are_refused(
    client, accounts, esm, totp, marker, url
) -> None:
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(True):
        response = _get(client, cookie, url)

    assert response.status_code == 403, response.text[:300]
    assert response.json()["detail"]["error"] == "elevation_required"
    assert marker not in response.text


@pytest.mark.parametrize("url", _ALL_ROUTES)
def test_log_routes_with_another_users_window_are_refused(
    client, accounts, esm, totp, marker, url
) -> None:
    admin, other = _admin(accounts, totp), _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    esm.create(session_key=cookie, username=other, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie, url)

    assert response.status_code in (303, 403), response.text[:300]
    assert marker not in response.text


# ---------------------------------------------------------------------------
# With a window, or with enforcement off: the logs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", _ALL_ROUTES)
def test_log_routes_with_elevation_serve_logs(
    client, accounts, esm, totp, marker, url
) -> None:
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie, url)

    assert response.status_code == 200, response.text[:300]
    assert marker in response.text


@pytest.mark.parametrize("url", _ALL_ROUTES)
def test_log_routes_with_enforcement_off_serve_logs(
    client, accounts, esm, totp, marker, url
) -> None:
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(False):
        response = _get(client, cookie, url)

    assert response.status_code == 200, response.text[:300]
    assert marker in response.text


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_export_names_the_file_for_the_page_script(
    client, accounts, esm, totp, marker, fmt
) -> None:
    """logs.html exportLogs() downloads via fetch() + a blob and names the
    file from Content-Disposition with /filename="([^"]+)"/."""
    import re

    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(False):
        response = _get(client, cookie, f"/admin/logs/export?format={fmt}")

    assert response.status_code == 200, response.text[:300]
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(
        rf'attachment; filename="logs_\d{{8}}_\d{{6}}\.{fmt}"', disposition
    )


def test_logs_page_serves_the_fetch_export_script(
    client, accounts, esm, totp, marker
) -> None:
    """The served page downloads the export through fetch() (which the
    elevation interceptor covers), never by navigating to it."""
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(False):
        response = _get(client, cookie, LOGS_PAGE)

    assert response.status_code == 200, response.text[:300]
    assert 'id="logs-export-status"' in response.text
    assert "fetch(exportUrl, { credentials: 'same-origin' })" in response.text
    assert "window.location.href = exportUrl" not in response.text


# ---------------------------------------------------------------------------
# Non-admin: refused as before
# ---------------------------------------------------------------------------


def test_logs_page_for_non_admin_is_refused(
    client, accounts, esm, totp, marker
) -> None:
    member = _member(accounts)
    cookie = _session_cookie(member, UserRole.NORMAL_USER)
    with enforcement(False):
        response = _get(client, cookie, LOGS_PAGE)

    assert response.status_code in (302, 303), response.text[:300]
    assert response.headers["location"].startswith("/login")
    assert marker not in response.text


@pytest.mark.parametrize("url", _GATED_READS)
def test_log_reads_for_non_admin_are_refused(
    client, accounts, esm, totp, marker, url
) -> None:
    member = _member(accounts)
    cookie = _session_cookie(member, UserRole.NORMAL_USER)
    esm.create(session_key=cookie, username=member, elevated_from_ip=None, scope="full")
    with enforcement(False):
        response = _get(client, cookie, url)

    assert response.status_code in (401, 403), response.text[:300]
    assert marker not in response.text
