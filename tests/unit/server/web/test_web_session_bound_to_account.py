"""A Web session authenticates only its live account, with the account's
CURRENT role.

Front door: the real app (``create_app``) with its real session manager and
account store (isolated per-session server home).  Sessions are issued by the
real session manager exactly as sign-in issues them.  Pages:

- ``/admin/users``: admin page (``_require_admin_session``)
- ``/user/api-keys``: any signed-in user (``_require_authenticated_session``)
- ``/admin/research``: admin page through the ``require_admin_session``
  dependency, which also performs the sliding session refresh
"""

from __future__ import annotations

import uuid
from http.cookies import SimpleCookie
from typing import Iterator, List

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import auth as web_auth
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Web-Session-Passw0rd!"
ADMIN_PAGE = "/admin/users"
USER_PAGE = "/user/api-keys"
SLIDING_ADMIN_PAGE = "/admin/research"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("web-session-app")) as app:
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _session(username: str, role: str, *, age_fraction: float = 0.0) -> str:
    """A Web session cookie issued for *username* by the real session
    manager; when *age_fraction* is set, the same payload is re-signed by
    that manager with its issue instant moved back by that fraction of the
    role's timeout (process time is never patched)."""
    sessions = web_auth.get_session_manager()
    response = Response()
    sessions.create_session(response, username, role)
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    value = cookie[web_auth.SESSION_COOKIE_NAME].value
    if not age_fraction:
        return value
    payload = sessions._serializer.loads(value, salt=sessions._salt)
    aged = payload["created_at"] - payload["session_timeout"] * age_fraction
    payload["created_at"] = payload["issued_at"] = aged
    signed: str = sessions._serializer.dumps(payload, salt=sessions._salt)
    return signed


def _get(client: TestClient, page: str, cookie: str):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    return client.get(page)


def _refused(response) -> bool:  # type: ignore[no-untyped-def]
    redirected = response.status_code in (302, 303, 307) and response.headers.get(
        "location", ""
    ).startswith("/login")
    return redirected or response.status_code in (401, 403)


def _reissued_session_cookies(response) -> List[str]:  # type: ignore[no-untyped-def]
    prefix = f"{web_auth.SESSION_COOKIE_NAME}="
    headers = response.headers
    values = (
        headers.get_list("set-cookie")  # httpx response
        if hasattr(headers, "get_list")
        else headers.getlist("set-cookie")  # Starlette Response
    )
    return [h for h in values if h.startswith(prefix)]


def test_live_admin_and_live_user_are_unaffected(client, accounts) -> None:
    admin, user = _name("admin"), _name("member")
    accounts.create_user(admin, PASSWORD, UserRole.ADMIN)
    accounts.create_user(user, PASSWORD, UserRole.NORMAL_USER)

    assert _get(client, ADMIN_PAGE, _session(admin, "admin")).status_code == 200
    assert _get(client, USER_PAGE, _session(admin, "admin")).status_code == 200
    assert _get(client, USER_PAGE, _session(user, "normal_user")).status_code == 200
    assert _refused(_get(client, ADMIN_PAGE, _session(user, "normal_user")))


def test_deleted_admins_session_is_refused_everywhere(client, accounts) -> None:
    admin = _name("admin")
    accounts.create_user(admin, PASSWORD, UserRole.ADMIN)
    cookie = _session(admin, "admin")
    assert accounts.delete_user_audited(admin, actor="example-admin")

    assert _refused(_get(client, ADMIN_PAGE, cookie))
    assert _refused(_get(client, USER_PAGE, cookie))
    assert _refused(_get(client, SLIDING_ADMIN_PAGE, cookie))


def test_demoted_admin_keeps_user_pages_only(client, accounts) -> None:
    admin = _name("admin")
    accounts.create_user(admin, PASSWORD, UserRole.ADMIN)
    cookie = _session(admin, "admin")
    assert accounts.update_user_role(admin, UserRole.NORMAL_USER)

    assert _refused(_get(client, ADMIN_PAGE, cookie))
    assert _refused(_get(client, SLIDING_ADMIN_PAGE, cookie))
    assert _get(client, USER_PAGE, cookie).status_code == 200


def test_session_issued_before_the_name_was_recreated_is_refused(
    client, accounts
) -> None:
    name = _name("admin")
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    cookie = _session(name, "admin")
    assert accounts.delete_user_audited(name, actor="example-admin")
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)

    assert _refused(_get(client, ADMIN_PAGE, cookie))
    assert _refused(_get(client, USER_PAGE, cookie))


def _backdate_account(accounts: UserManager, username: str) -> None:
    """Record *username*'s account as created a day ago, so its own aged
    sessions are legitimately newer than the account."""
    import sqlite3
    from contextlib import closing
    from datetime import datetime, timedelta, timezone

    created = datetime.now(timezone.utc) - timedelta(days=1)
    backend = accounts._sqlite_backend
    assert backend is not None
    with closing(sqlite3.connect(backend._conn_manager.db_path)) as conn:
        conn.execute(
            "UPDATE users SET account_created_at = ? WHERE username = ?",
            (created.isoformat(), username),
        )
        conn.commit()
    account = accounts.get_user(username)
    assert account is not None and account.account_created_at == created


def _refresh(cookie: str):  # type: ignore[no-untyped-def]
    """Run the production session manager's sliding refresh on *cookie*."""
    from starlette.requests import Request

    header = f"{web_auth.SESSION_COOKIE_NAME}={cookie}".encode()
    request = Request({"type": "http", "headers": [(b"cookie", header)]})
    response = Response()
    session = web_auth.get_session_manager().get_and_refresh_session(request, response)
    return session, _reissued_session_cookies(response)


def test_sliding_refresh_reissues_only_sessions_of_live_accounts(
    client, accounts
) -> None:
    """Checked on the session manager the guards use (a page returning its
    own HTMLResponse does not carry the dependency's cookie)."""
    live, gone = _name("admin"), _name("admin")
    accounts.create_user(live, PASSWORD, UserRole.ADMIN)
    accounts.create_user(gone, PASSWORD, UserRole.ADMIN)
    _backdate_account(accounts, live)  # aged sessions are newer than both
    _backdate_account(accounts, gone)
    live_cookie = _session(live, "admin", age_fraction=0.75)
    gone_cookie = _session(gone, "admin", age_fraction=0.75)
    assert accounts.delete_user_audited(gone, actor="example-admin")

    live_session, live_reissued = _refresh(live_cookie)
    gone_session, gone_reissued = _refresh(gone_cookie)

    assert live_session is not None and len(live_reissued) == 1
    assert gone_session is None and gone_reissued == []


def test_aged_session_of_deleted_account_is_refused_by_refreshing_guard(
    client, accounts
) -> None:
    """The refreshing guard refuses a refresh-due session of a deleted
    account (no-reissue is asserted on the session manager above)."""
    admin = _name("admin")
    accounts.create_user(admin, PASSWORD, UserRole.ADMIN)
    _backdate_account(accounts, admin)
    aged = _session(admin, "admin", age_fraction=0.75)
    assert accounts.delete_user_audited(admin, actor="example-admin")

    assert _refused(_get(client, SLIDING_ADMIN_PAGE, aged))


ASYNC_ADMIN_PAGE = "/admin/api/discovery/example/result/example-job"


def test_async_page_resolves_the_account_off_the_event_loop(client, accounts) -> None:
    """An ``async def`` page binds the session without blocking the loop."""
    import asyncio

    sessions = web_auth.get_session_manager()
    real_lookup = sessions._account_lookup
    on_loop: List[bool] = []

    def recording_lookup(username: str):  # type: ignore[no-untyped-def]
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_lookup(username)  # type: ignore[misc]

    admin, gone = _name("admin"), _name("admin")
    accounts.create_user(admin, PASSWORD, UserRole.ADMIN)
    accounts.create_user(gone, PASSWORD, UserRole.ADMIN)
    gone_cookie = _session(gone, "admin")
    assert accounts.delete_user_audited(gone, actor="example-admin")
    sessions._account_lookup = recording_lookup
    try:
        live = _get(client, ASYNC_ADMIN_PAGE, _session(admin, "admin"))
        refused = _get(client, ASYNC_ADMIN_PAGE, gone_cookie)
    finally:
        sessions._account_lookup = real_lookup

    assert not _refused(live), live.status_code
    assert refused.status_code == 401
    assert on_loop and not any(on_loop), on_loop
