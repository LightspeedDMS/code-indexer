"""
Regression tests for Bug #1283: Web UI "Create API Key" returns 401.

Root cause: POST/GET/DELETE /api/keys (inline_auth.py) depended on
dependencies.get_current_user, which reads ONLY the Authorization: Bearer
header. The Web UI /user/api-keys page authenticates via the web `session`
cookie and never sends a Bearer header, so a logged-in user was rejected
with 401 "Missing authentication credentials".

Fix: swap the dependency to dependencies.get_current_user_hybrid, which
accepts EITHER the Bearer header OR the web session cookie (already the
pattern used by inline_jobs.py / repository_health.py).

These tests exercise the real FastAPI routes registered by
register_auth_routes() through TestClient — no mocking of the route
handlers or the hybrid-auth dependency itself.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Eagerly import code_indexer.server.app BEFORE any test wires
# auth_deps.user_manager/jwt_manager. server/app.py performs a module-level
# `app = create_app()` which calls initialize_services() and assigns REAL
# instances into dependencies.user_manager/jwt_manager as a side effect.
# dependencies._validate_jwt_and_get_user() does a lazy
# `from code_indexer.server.app import is_token_blacklisted` — if that is
# the FIRST import of the module, it fires mid-request and clobbers our
# test mocks. Importing it here up front means the clobbering happens once,
# before each test's fixture wires its own mocks (see also #1181-adjacent
# module-import-order footguns).
import code_indexer.server.app  # noqa: F401

from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.auth import dependencies as auth_deps
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.web.auth import SessionData


def _stop_and_clear_governor_now() -> None:
    """Bug #1447: stop() whatever MemoryGovernor is currently installed and
    clear the process-wide singleton, RIGHT NOW (synchronously).

    The `import code_indexer.server.app` line above triggers that module's
    top-level `app = create_app()`, which calls initialize_services() and
    installs a REAL MemoryGovernor with a REAL sampler thread
    (services/memory_governor.py) as an unavoidable side effect -- nothing
    else in the suite ever tears that down. Left alone, it keeps sampling
    real system memory for the rest of the single-process pytest chunk and
    can end up denying admission for an unrelated heavy job
    (BackgroundJobManager._admission_blocked) run by a completely different
    test later in the same process.

    This must run EAGERLY, at module-import time, rather than being
    deferred to a fixture: pytest imports ALL collected test modules during
    COLLECTION, before ANY test in ANY file runs. A fixture-based teardown
    only fires after THIS module's own tests finish, which is too late if
    some other module elsewhere in the same directory happens to be
    collected (and executed) earlier -- the leaked real governor would
    still be live during that whole window. Calling this function
    immediately, at import time (below), closes that window entirely
    regardless of collection/execution order.
    """
    from code_indexer.server.services.memory_governor import (
        get_memory_governor,
        clear_memory_governor,
    )

    governor = get_memory_governor()
    if governor is not None:
        governor.stop()
    clear_memory_governor()


_stop_and_clear_governor_now()


_SESSION_COOKIE_VALUE = "session-fixture-value"
_TEST_JWT_SECRET = "test-secret-key-1283"


def test_stop_and_clear_governor_now_stops_and_clears_installed_governor():
    """Bug #1447 RED: _stop_and_clear_governor_now() -- a plain, synchronous
    (non-fixture, non-generator) function -- must call stop() on whatever
    MemoryGovernor is installed and then clear the singleton.

    It must be plain and synchronous (not a fixture) because pytest imports
    ALL collected test modules during COLLECTION, before ANY test in ANY
    file runs. A fixture-based teardown only fires after THIS module's own
    tests finish, which is too late if an unrelated module elsewhere in the
    same directory happens to be collected (and its tests run) earlier --
    the leaked real governor would still be live during that window. Calling
    this function eagerly, immediately after the
    `import code_indexer.server.app` line below, closes that window
    entirely regardless of collection/execution order.
    """
    from code_indexer.server.services import memory_governor as mg

    class _StubGovernor:
        def __init__(self):
            self.stopped = False

        def stop(self, timeout: float = 5.0) -> None:
            self.stopped = True

    stub = _StubGovernor()
    mg.set_memory_governor(stub)
    try:
        _stop_and_clear_governor_now()

        assert stub.stopped is True, "stop() must be called on the installed governor"
        assert mg.get_memory_governor() is None, (
            "the governor singleton must be cleared"
        )
    finally:
        mg.clear_memory_governor()


@pytest.fixture(autouse=True)
def _restore_auth_deps_globals():
    """dependencies.py holds module-level globals (jwt_manager, user_manager,
    oauth_manager, api_key_manager) that these tests must wire and MUST NOT
    leak into other test modules that share the same process."""
    saved = (
        auth_deps.jwt_manager,
        auth_deps.user_manager,
        auth_deps.oauth_manager,
        auth_deps.api_key_manager,
    )
    yield
    (
        auth_deps.jwt_manager,
        auth_deps.user_manager,
        auth_deps.oauth_manager,
        auth_deps.api_key_manager,
    ) = saved


def _make_app():
    """Build a minimal FastAPI app with only the auth/api-key routes registered."""
    app = FastAPI()
    mock_jwt = MagicMock()
    mock_user_mgr = MagicMock()
    mock_user_mgr.get_api_keys.return_value = []
    mock_refresh_mgr = MagicMock()

    register_auth_routes(
        app,
        jwt_manager=mock_jwt,
        user_manager=mock_user_mgr,
        refresh_token_manager=mock_refresh_mgr,
    )
    return app, mock_user_mgr, mock_jwt


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="placeholder-hash-not-real",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _session_client(app, user_manager_mock, user: User):
    """Wire dependencies.user_manager + a fake session manager so a
    session-cookie-only request authenticates to `user` via the hybrid path.

    dependencies.get_current_user (the Bearer-only path used before the fix)
    also requires a non-None jwt_manager/user_manager just to reach its
    "missing credentials" 401 branch, so those globals are wired here too.
    """
    auth_deps.jwt_manager = JWTManager(secret_key=_TEST_JWT_SECRET)
    auth_deps.oauth_manager = None
    auth_deps.api_key_manager = None
    auth_deps.user_manager = user_manager_mock
    user_manager_mock.get_user.return_value = user

    session_data = SessionData(
        username=user.username,
        role=user.role.value,
        csrf_token="csrf-fixture",
        created_at=datetime.now(timezone.utc).timestamp(),
    )
    mock_session_manager = MagicMock()
    mock_session_manager.get_session.return_value = session_data

    patcher = patch(
        "code_indexer.server.web.auth.get_session_manager",
        return_value=mock_session_manager,
    )
    patcher.start()
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session", _SESSION_COOKIE_VALUE)
    return client, patcher


class TestCreateApiKeySessionAuth:
    """POST /api/keys authenticated via web session cookie only (no Bearer)."""

    def test_post_api_keys_with_session_cookie_returns_201(self):
        app, user_manager_mock, _ = _make_app()
        user = _make_user("alice", UserRole.ADMIN)
        client, patcher = _session_client(app, user_manager_mock, user)
        try:
            resp = client.post("/api/keys", json={"name": "AICC"})
        finally:
            patcher.stop()

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "api_key" in body
        assert body["name"] == "AICC"


class TestCreateApiKeyBearerAuthRegression:
    """Regression: Bearer-token POST /api/keys must still work after the swap."""

    def test_post_api_keys_with_bearer_token_returns_201(self):
        app, user_manager_mock, _ = _make_app()
        user_manager_mock.get_api_keys.return_value = []

        bearer_user = _make_user("bob", UserRole.ADMIN)

        auth_deps.jwt_manager = JWTManager(secret_key=_TEST_JWT_SECRET)
        auth_deps.oauth_manager = None
        auth_deps.api_key_manager = None
        auth_deps.user_manager = user_manager_mock
        user_manager_mock.get_user.return_value = bearer_user

        token = auth_deps.jwt_manager.create_token(
            {
                "username": bearer_user.username,
                "role": bearer_user.role.value,
                "created_at": bearer_user.created_at.isoformat(),
            }
        )

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/keys",
            json={"name": "cli-key"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "api_key" in body
        assert body["name"] == "cli-key"


class TestCreateApiKeyNormalUserOwnKey:
    """A Normal (non-admin) user creating their OWN key via session must
    succeed — the 401 was an identity failure, not a permission gate, so
    the hybrid dependency (which is non-admin) must not introduce a 403."""

    def test_normal_user_session_creates_own_key_201_not_403(self):
        app, user_manager_mock, _ = _make_app()
        normal_user = _make_user("carol", UserRole.NORMAL_USER)
        client, patcher = _session_client(app, user_manager_mock, normal_user)
        try:
            resp = client.post("/api/keys", json={"name": "my-key"})
        finally:
            patcher.stop()

        assert resp.status_code == 201, resp.text
        assert resp.status_code != 403


class TestListApiKeysSessionAuth:
    """GET /api/keys authenticated via web session cookie only (no Bearer)."""

    def test_get_api_keys_with_session_cookie_returns_200(self):
        app, user_manager_mock, _ = _make_app()
        user_manager_mock.get_api_keys.return_value = [
            {
                "key_id": "kid-1",
                "name": "existing",
                "created_at": "2026-01-01T00:00:00Z",
                "key_prefix": "cidx_sk_abcd",
            }
        ]
        user = _make_user("dave", UserRole.ADMIN)
        client, patcher = _session_client(app, user_manager_mock, user)
        try:
            resp = client.get("/api/keys")
        finally:
            patcher.stop()

        assert resp.status_code == 200, resp.text
        assert resp.json()["keys"][0]["key_id"] == "kid-1"


class TestDeleteApiKeySessionAuth:
    """DELETE /api/keys/{key_id} authenticated via web session cookie only (no Bearer)."""

    def test_delete_api_key_with_session_cookie_returns_200(self):
        app, user_manager_mock, _ = _make_app()
        user_manager_mock.delete_api_key.return_value = True
        user = _make_user("erin", UserRole.ADMIN)
        client, patcher = _session_client(app, user_manager_mock, user)
        try:
            resp = client.delete("/api/keys/kid-1")
        finally:
            patcher.stop()

        assert resp.status_code == 200, resp.text
        user_manager_mock.delete_api_key.assert_called_once_with("erin", "kid-1")
