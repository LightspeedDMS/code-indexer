"""Admin reads of user data and search history require TOTP elevation.

``GET /api/admin/users`` and the search-event history routes
(``GET /api/admin/search-events``, ``POST /api/admin/search-events/export``,
``GET /api/admin/search-events/exports`` and its download) answer 403
``elevation_required`` to an admin without their own elevation window when
enforcement is on, exactly like their gated twins (``GET /api/v1/users``,
MCP ``list_users``).  An admin API key cannot hold a window, so it is refused
too.  With the caller's own window -- keyed by a Bearer JWT's jti or by the
Web session cookie -- they serve as before; with enforcement off nothing
changes.

Front door: the real app (``create_app`` via ``isolated_app``, never
~/.cidx-server) over its real auth components.  The search-event history and
the export registry are real SQLite stores on per-test files; elevation
windows and TOTP enrolment are real stores on per-test files.  Only the
elevation-enforcement switch is patched.
"""

from __future__ import annotations

import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pyotp
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.api_key_manager import ApiKeyManager
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
    QueryAnalyticsExportSqliteBackend,
)
from code_indexer.server.services.search_event_log_writer import (
    SearchEventLogSqliteBackend,
    SearchEventLogWriter,
    SearchEventRecord,
)
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-Reads-Passw0rd!"
EXPORT_ID = str(uuid.uuid4())

USERS = "/api/admin/users"
EVENTS = "/api/admin/search-events"
EXPORTS = "/api/admin/search-events/exports"
DOWNLOAD = f"/api/admin/search-events/exports/{EXPORT_ID}/download"
EXPORT = "/api/admin/search-events/export"

# Every gated admin read: (method, path).
_GATED = [
    pytest.param("GET", USERS, id="users"),
    pytest.param("GET", EVENTS, id="search-events"),
    pytest.param("GET", EXPORTS, id="exports"),
    pytest.param("GET", DOWNLOAD, id="download"),
    pytest.param("POST", EXPORT, id="export"),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("admin-reads-app")) as app:
        yield TestClient(app, follow_redirects=False)


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


@pytest.fixture
def secrets(
    client: TestClient,
    accounts: UserManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, Optional[str]]:
    """Real user, search-history and export rows; returns, per path, a value
    only that route's successful response carries (None: the export trigger,
    whose success is its 202)."""
    member = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(member, PASSWORD, UserRole.NORMAL_USER)

    query_text = f"example-query-{uuid.uuid4().hex[:12]}"
    events = SearchEventLogSqliteBackend(str(tmp_path / "search_events.db"))
    events.insert_batch(
        [
            SearchEventRecord(
                timestamp=time.time(),
                username=member,
                repo_alias="example-repo-global",
                search_type="semantic",
                query_text=query_text,
                voyage_cache_hit=None,
                voyage_cache_mode=None,
                voyage_latency_ms=None,
                cohere_cache_hit=None,
                cohere_cache_mode=None,
                cohere_latency_ms=None,
                total_latency_ms=1,
                result_count=1,
                node_id="example-node",
                correlation_id=None,
            )
        ]
    )

    export_body = f"example-export-{uuid.uuid4().hex[:12]}"
    export_file = tmp_path / "export.xlsx"
    export_file.write_text(export_body)
    exports = QueryAnalyticsExportSqliteBackend(str(tmp_path / "exports.db"))
    exports.create_export(
        {
            "id": EXPORT_ID,
            "initiated_by": "example-admin",
            "created_at": time.time(),
            "status": "completed",
            "filter_summary": "all",
            "file_path": str(export_file),
            "file_size_bytes": len(export_body),
            "row_count": 1,
        }
    )

    state = client.app.state  # type: ignore[attr-defined]
    monkeypatch.setattr(
        state, "search_event_log_writer", SearchEventLogWriter(events), raising=False
    )
    monkeypatch.setattr(
        state,
        "query_analytics_export_service",
        QueryAnalyticsExportService(exports, golden_repos_dir=str(tmp_path)),
        raising=False,
    )
    return {
        USERS: member,
        EVENTS: query_text,
        EXPORTS: EXPORT_ID,
        DOWNLOAD: export_body,
        EXPORT: None,
    }


def _admin(accounts: UserManager, totp: TOTPService) -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    return name


def _bearer(username: str) -> tuple:
    """(jwt, jti) for an admin -- the jti keys a Bearer caller's window."""
    assert dependencies.jwt_manager is not None
    token = dependencies.jwt_manager.create_token(
        {"username": username, "role": UserRole.ADMIN.value}
    )
    jti = dependencies.jwt_manager.validate_token(token)["jti"]
    return token, str(jti)


def _session_cookie(username: str) -> str:
    response = Response()
    web_auth.get_session_manager().create_session(
        response, username, UserRole.ADMIN.value
    )
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


def _call(
    client: TestClient,
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    cookie: Optional[str] = None,
) -> Any:
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if cookie:
        client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    try:
        if method == "POST":
            return client.post(path, json={}, headers=headers)
        return client.get(path, headers=headers)
    finally:
        client.cookies.clear()


def _assert_refused(response: Any, secret: Optional[str]) -> None:
    assert response.status_code == 403, response.text[:300]
    assert response.json()["detail"]["error"] == "elevation_required"
    if secret is not None:
        assert secret not in response.text


def _assert_served(method: str, response: Any, secret: Optional[str]) -> None:
    if method == "POST":
        assert response.status_code == 202, response.text[:300]
        assert response.json()["job_id"]
        return
    assert response.status_code == 200, response.text[:300]
    assert secret is not None and secret in response.text


# ---------------------------------------------------------------------------
# Enforcement on, no window of the caller's own: refused, nothing disclosed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_without_window_is_refused(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    token, _jti = _bearer(_admin(accounts, totp))
    with enforcement(True):
        response = _call(client, method, path, token=token)
    _assert_refused(response, secrets[path])


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_with_another_admins_window_is_refused(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    admin, other = _admin(accounts, totp), _admin(accounts, totp)
    token, jti = _bearer(admin)
    esm.create(session_key=jti, username=other, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _call(client, method, path, token=token)
    _assert_refused(response, secrets[path])


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_with_admin_api_key_is_refused(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    """An API key carries no elevation window, so it never reads these."""
    admin = _admin(accounts, totp)
    raw_key, _key_id = ApiKeyManager(user_manager=accounts).generate_key(admin)
    with enforcement(True):
        response = _call(client, method, path, token=raw_key)
    _assert_refused(response, secrets[path])


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_through_web_session_without_window_is_refused(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    cookie = _session_cookie(_admin(accounts, totp))
    with enforcement(True):
        response = _call(client, method, path, cookie=cookie)
    _assert_refused(response, secrets[path])


# ---------------------------------------------------------------------------
# The caller's own window, or enforcement off: served as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_with_own_bearer_window_is_served(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    admin = _admin(accounts, totp)
    token, jti = _bearer(admin)
    esm.create(session_key=jti, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _call(client, method, path, token=token)
    _assert_served(method, response, secrets[path])


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_with_own_web_session_window_is_served(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    """The analytics export page calls these with its session cookie."""
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _call(client, method, path, cookie=cookie)
    _assert_served(method, response, secrets[path])


@pytest.mark.parametrize("method,path", _GATED)
def test_admin_read_with_enforcement_off_is_served(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    token, _jti = _bearer(_admin(accounts, totp))
    with enforcement(False):
        response = _call(client, method, path, token=token)
    _assert_served(method, response, secrets[path])


# ---------------------------------------------------------------------------
# Other admin reads of user data and audit content, gated the same way
# ---------------------------------------------------------------------------

_ALSO_GATED = [
    pytest.param("GET", "/api/admin/activated-repos", id="activated-repos"),
    pytest.param("GET", "/api/v1/groups/1", id="group-members"),
    pytest.param("GET", "/api/admin/scip-pr-history", id="pr-audit-history"),
    pytest.param("GET", "/api/admin/scip-git-cleanup-history", id="cleanup-audit"),
]
_ALL_CHANGED = _GATED + _ALSO_GATED


@pytest.mark.parametrize("method,path", _ALSO_GATED)
def test_admin_read_also_gated_without_window_is_refused(
    client, accounts, esm, totp, method, path
) -> None:
    token, _jti = _bearer(_admin(accounts, totp))
    with enforcement(True):
        response = _call(client, method, path, token=token)
    _assert_refused(response, None)


# ---------------------------------------------------------------------------
# A non-admin is refused whether or not they hold a window of their own
# ---------------------------------------------------------------------------


def _member_bearer(accounts: UserManager, totp: TOTPService) -> tuple:
    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    assert dependencies.jwt_manager is not None
    token = dependencies.jwt_manager.create_token(
        {"username": name, "role": UserRole.NORMAL_USER.value}
    )
    jti = dependencies.jwt_manager.validate_token(token)["jti"]
    return name, token, str(jti)


@pytest.mark.parametrize("method,path", _ALL_CHANGED)
def test_normal_user_is_refused_without_window(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    _name, token, _jti = _member_bearer(accounts, totp)
    with enforcement(True):
        response = _call(client, method, path, token=token)
    assert response.status_code == 403, response.text[:300]
    if secrets.get(path):
        assert str(secrets[path]) not in response.text


@pytest.mark.parametrize("method,path", _ALL_CHANGED)
def test_normal_user_is_refused_with_own_window(
    client, accounts, esm, totp, secrets, method, path
) -> None:
    """An elevation window never grants the admin role."""
    name, token, jti = _member_bearer(accounts, totp)
    esm.create(session_key=jti, username=name, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _call(client, method, path, token=token)
    assert response.status_code == 403, response.text[:300]
    assert response.json()["detail"] != {"error": "elevation_required"}
    if secrets.get(path):
        assert str(secrets[path]) not in response.text
