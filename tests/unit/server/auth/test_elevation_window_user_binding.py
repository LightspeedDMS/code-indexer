"""
Tests proving an elevation window is valid only for the user who created it.

Elevation windows (Story #923) are looked up by session key -- the JWT jti for
Bearer auth, or the cidx_session cookie value for Web UI auth. A session key
alone does not prove which user is making the CURRENT request: a lookup must
also confirm the window's stored username matches the user this request
actually authenticated as. A mismatch must be treated exactly like "no
window" (elevation_required), never granted.

Covers:
- require_elevation() (REST admin gate, auth/dependencies.py)
- a real front-door REST route (POST /api/admin/users) exercised via
  TestClient + dependency_overrides, matching this repo's established
  elevation-test pattern (see test_admin_users_elevation_required.py)
- the MCP decorator (mcp/auth/elevation_decorator.py) -- already user-bound;
  kept here as a same-invariant regression guard
- kill switch and window expiry are unaffected by the user-binding check
- the real auth dependency chain (get_current_admin_user_hybrid and
  get_current_user run unmodified) for both an API-key-authenticated
  caller and a web-session-authenticated caller, including a mutation
  check proving the denial depends on the user-bound lookup rather than
  on test-harness incidentals
- the fail-closed fallback in _check_session_window() when no username is
  resolvable from request.state
"""

import contextlib
import json
import tempfile
import time
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.password_manager import PasswordManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_SESSION_KEY = "test-session-key-binding"
_USER_A = "user_a_binding"
_USER_B = "user_b_binding"
_IP = "127.0.0.1"
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800
_HTTP_403 = 403

_ENFORCEMENT_ENABLED_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Real ElevatedSessionManager (SQLite) for testing."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_TIMEOUT,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elevated_sessions.db"),
    )


def _make_user(username: str) -> User:
    return User(
        username=username,
        role=UserRole.ADMIN,
        password_hash=PasswordManager().hash_password("testpassword"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def user_a():
    return _make_user(_USER_A)


@pytest.fixture
def user_b():
    return _make_user(_USER_B)


@pytest.fixture(autouse=True)
def _restore_manager():
    """Restore module-level elevated_session_manager after each test."""
    original = _deps.elevated_session_manager
    yield
    _deps.elevated_session_manager = original


@contextlib.contextmanager
def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_ENFORCEMENT_ENABLED_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        yield


def _make_request(jti) -> MagicMock:
    request = MagicMock()
    request.state.user_jti = jti
    request.cookies = {}
    return request


def _make_cookie_request(cookie_value: str) -> MagicMock:
    request = MagicMock()
    request.state.user_jti = None
    request.cookies = {"cidx_session": cookie_value}
    return request


def _run_dep(request, user, manager, required_scope: str = "full"):
    _deps.elevated_session_manager = manager
    with _elevation_ctx(enforcement=True):
        dep = _deps.require_elevation(required_scope=required_scope)
        return dep(request, user)


# ---------------------------------------------------------------------------
# require_elevation() -- direct dependency invocation
# ---------------------------------------------------------------------------


def test_require_elevation_rejects_window_opened_by_a_different_user(
    manager, user_a, user_b
):
    """A window opened by user_a must not satisfy require_elevation() for user_b."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user_b, manager)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]


def test_require_elevation_rejects_window_opened_by_a_different_user_via_cookie(
    manager, user_a, user_b
):
    """Same invariant when the session key resolves via the cidx_session cookie
    fallback rather than request.state.user_jti."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_cookie_request(_SESSION_KEY), user_b, manager)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]


def test_require_elevation_accepts_the_creating_users_own_window(manager, user_a):
    """Regression guard: the user who opened the window is still elevated."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    result = _run_dep(_make_request(_SESSION_KEY), user_a, manager)
    assert result is user_a


def test_require_elevation_kill_switch_off_ignores_user_binding(
    manager, user_a, user_b
):
    """Kill switch is evaluated before the user-binding check: OFF means
    passthrough for every user, matched or not."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    _deps.elevated_session_manager = manager
    with _elevation_ctx(enforcement=False):
        dep = _deps.require_elevation()
        result = dep(_make_request(_SESSION_KEY), user_b)
    assert result is user_b


def test_require_elevation_still_expires_the_creating_users_own_window(manager, user_a):
    """Window expiry is unaffected by user-binding: a revoked window denies
    even its own creator."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    manager.revoke(_SESSION_KEY)
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user_a, manager)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Real front door: POST /api/admin/users, behind require_elevation()
# ---------------------------------------------------------------------------


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def _get_app(tmpdir: str):
    """Lazy-import app with isolated DB to avoid collection-time DB lock."""
    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}):
        reset_config_service()
        from code_indexer.server.app import app as _app

        return _app


def test_rest_route_rejects_window_opened_by_a_different_authenticated_user(
    manager, user_b, tmpdir_path
):
    """POST /api/admin/users, authenticated as user_b, presenting a session
    key whose elevation window belongs to user_a, must return 403
    elevation_required rather than performing the mutation."""
    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USER_A, _IP)
    app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: user_b
    try:
        client = TestClient(app, raise_server_exceptions=False)
        with _elevation_ctx(enforcement=True):
            resp = client.post(
                "/api/admin/users",
                json={
                    "username": "irrelevant",
                    "password": "Xk9$vLp2Qz#8Ymw5Tr!",
                    "role": "normal_user",
                },
                cookies={"cidx_session": _SESSION_KEY},
            )
    finally:
        app.dependency_overrides.pop(_deps.get_current_admin_user_hybrid, None)
    assert resp.status_code == _HTTP_403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


def test_rest_route_accepts_window_opened_by_the_same_authenticated_user(
    manager, user_a, tmpdir_path
):
    """Regression guard: the front-door route still succeeds for the
    legitimate owner of the elevation window."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _find_route_handler,
        _patch_closure,
    )

    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USER_A, _IP)
    app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: user_a
    handler = _find_route_handler("/api/admin/users", "POST")
    mock_um = Mock()
    mock_um.create_user.return_value = User(
        username="newuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        with _elevation_ctx(enforcement=True):
            with _patch_closure(handler, "user_manager", mock_um):
                resp = client.post(
                    "/api/admin/users",
                    json={
                        "username": "newuser",
                        "password": "P@ss1234!",
                        "role": "normal_user",
                    },
                    cookies={"cidx_session": _SESSION_KEY},
                )
    finally:
        app.dependency_overrides.pop(_deps.get_current_admin_user_hybrid, None)
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# MCP decorator -- same invariant, already correctly user-bound.
# Kept as a regression guard: any future refactor of require_mcp_elevation()
# that regresses to a bare touch_atomic() must fail this test.
# ---------------------------------------------------------------------------

_MCP_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_MCP_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_MCP_ESM_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
)


def _mcp_noop_handler(args, user, session_key=None):
    return {"success": True}


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


@contextlib.contextmanager
def _mcp_patch_all(esm, mfa_enabled: bool = True, enforcement: bool = True):
    totp_svc = MagicMock()
    totp_svc.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_MCP_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_MCP_ESM_PATH, esm),
        patch(_MCP_TOTP_PATH, return_value=totp_svc),
    ):
        yield


def test_mcp_decorator_rejects_window_opened_by_a_different_user(
    manager, user_a, user_b
):
    """A window opened by user_a must not satisfy @require_mcp_elevation() for
    a call authenticated as user_b."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    decorated = require_mcp_elevation()(_mcp_noop_handler)
    with _mcp_patch_all(manager):
        result = decorated({}, user_b, session_key=_SESSION_KEY)
    parsed = _parse_mcp_response(result)
    assert parsed["error"] == "elevation_required"


def test_mcp_decorator_accepts_the_creating_users_own_window(manager, user_a):
    """Regression guard: the MCP decorator still passes for the legitimate
    owner of the elevation window."""
    manager.create(_SESSION_KEY, _USER_A, _IP)
    decorated = require_mcp_elevation()(_mcp_noop_handler)
    with _mcp_patch_all(manager):
        result = decorated({}, user_a, session_key=_SESSION_KEY)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Permanent regression: the user-binding invariant, through the REAL auth
# chain.
#
# get_current_admin_user_hybrid and get_current_user run completely
# unmodified -- only the identity-provider boundary (API-key verification,
# the user store, and the web-session lookup) is stubbed, the same boundary
# a real deployment's pluggable auth backends sit behind.
# ---------------------------------------------------------------------------

# "cidx_sk_" mirrors ApiKeyManager.KEY_PREFIX (auth/api_key_manager.py) --
# a plain string literal here avoids pulling that module into this file's
# import graph for a single stable, well-known prefix constant.
_API_KEY_TOKEN = "cidx_sk_test-key-user-b-binding"
_WEB_SESSION_COOKIE_VALUE = "opaque-web-session-cookie-value-binding"
_STRONG_PASSWORD = "Xk9$vLp2Qz#8Ymw5Tr!"


@contextlib.contextmanager
def _stub_api_key_identity(user: User):
    """Stub only the API-key verifier and the user store so the real
    get_current_admin_user_hybrid -> get_current_user chain runs unmodified
    for everything else (JWT/OAuth dispatch, admin-permission check,
    non-SSO restriction, elevation gate)."""
    original_api_key_manager = _deps.api_key_manager
    original_user_manager = _deps.user_manager
    original_server_config = _deps.server_config
    stub_api_key_manager = MagicMock()
    stub_api_key_manager.authenticate_bearer.return_value = user
    stub_user_manager = MagicMock()
    stub_user_manager.get_user.return_value = user
    stub_user_manager.is_sso_user.return_value = True
    _deps.api_key_manager = stub_api_key_manager
    _deps.user_manager = stub_user_manager
    _deps.server_config = None
    try:
        yield
    finally:
        _deps.api_key_manager = original_api_key_manager
        _deps.user_manager = original_user_manager
        _deps.server_config = original_server_config


def test_real_auth_chain_api_key_user_with_cross_user_cookie_is_denied(
    manager, user_a, user_b, tmpdir_path
):
    """API-key caller with another user's window key in the cookie is
    denied, through the real auth chain: user_b authenticates with their
    own API key (Authorization: Bearer cidx_sk_...) and presents a
    cidx_session cookie carrying user_a's window key. Only the API-key
    verifier and the user store are stubbed -- get_current_admin_user_hybrid
    and get_current_user run for real. POST /api/admin/users must return 403
    elevation_required, never perform the mutation."""
    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USER_A, _IP)
    with _stub_api_key_identity(user_b):
        client = TestClient(app, raise_server_exceptions=False)
        with _elevation_ctx(enforcement=True):
            resp = client.post(
                "/api/admin/users",
                json={
                    "username": "irrelevant",
                    "password": _STRONG_PASSWORD,
                    "role": "normal_user",
                },
                headers={"Authorization": f"Bearer {_API_KEY_TOKEN}"},
                cookies={"cidx_session": _SESSION_KEY},
            )
    assert resp.status_code == _HTTP_403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


def test_real_auth_chain_web_session_with_cross_user_window_is_denied(
    manager, user_a, user_b, tmpdir_path
):
    """Same invariant via the Web UI session-cookie auth path. The elevation
    window is keyed by the "session" cookie's own raw value -- exactly how
    _hybrid_auth_impl derives the elevation session key for a
    session-authenticated caller -- but owned by user_a. Only the session
    lookup and the user store are stubbed; get_current_admin_user_hybrid
    runs for real."""
    from code_indexer.server.web.auth import SessionData

    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_WEB_SESSION_COOKIE_VALUE, _USER_A, _IP)

    fake_session_manager = MagicMock()
    fake_session_manager.get_session.return_value = SessionData(
        username=_USER_B,
        role="admin",
        csrf_token="test-csrf-token",
        created_at=time.time(),
    )
    original_user_manager = _deps.user_manager
    stub_user_manager = MagicMock()
    stub_user_manager.get_user.return_value = user_b
    _deps.user_manager = stub_user_manager
    try:
        with patch(
            "code_indexer.server.web.auth.get_session_manager",
            return_value=fake_session_manager,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            with _elevation_ctx(enforcement=True):
                resp = client.post(
                    "/api/admin/users",
                    json={
                        "username": "irrelevant",
                        "password": _STRONG_PASSWORD,
                        "role": "normal_user",
                    },
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )
    finally:
        _deps.user_manager = original_user_manager
    assert resp.status_code == _HTTP_403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


def test_real_auth_chain_api_key_users_own_window_still_succeeds(
    manager, user_b, tmpdir_path
):
    """Regression guard: the real auth chain still succeeds when the
    API-key caller's own window is presented (the legitimate case)."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _find_route_handler,
        _patch_closure,
    )

    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USER_B, _IP)
    handler = _find_route_handler("/api/admin/users", "POST")
    mock_um = Mock()
    mock_um.create_user.return_value = User(
        username="newuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    with _stub_api_key_identity(user_b):
        client = TestClient(app, raise_server_exceptions=False)
        with _elevation_ctx(enforcement=True):
            with _patch_closure(handler, "user_manager", mock_um):
                resp = client.post(
                    "/api/admin/users",
                    json={
                        "username": "newuser",
                        "password": "P@ss1234!",
                        "role": "normal_user",
                    },
                    headers={"Authorization": f"Bearer {_API_KEY_TOKEN}"},
                    cookies={"cidx_session": _SESSION_KEY},
                )
    assert resp.status_code == 201, resp.text


def test_real_auth_chain_cross_user_denial_depends_on_user_bound_lookup(
    manager, user_a, user_b, tmpdir_path
):
    """Mutation check: the cross-user denial proven above is not a tautology
    of the test harness -- it depends on touch_atomic_for_user's username
    binding. Swapping touch_atomic_for_user to delegate to the unqualified
    touch_atomic() in memory must make the exact same request succeed
    instead of being denied."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _find_route_handler,
        _patch_closure,
    )

    app = _get_app(tmpdir_path)
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USER_A, _IP)

    manager_cls = type(manager)
    original_touch_atomic_for_user = manager_cls.touch_atomic_for_user
    manager_cls.touch_atomic_for_user = (  # type: ignore[method-assign]
        lambda self, session_key, username: self.touch_atomic(session_key)
    )

    handler = _find_route_handler("/api/admin/users", "POST")
    mock_um = Mock()
    mock_um.create_user.return_value = User(
        username="irrelevant",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        with _stub_api_key_identity(user_b):
            client = TestClient(app, raise_server_exceptions=False)
            with _elevation_ctx(enforcement=True):
                with _patch_closure(handler, "user_manager", mock_um):
                    resp = client.post(
                        "/api/admin/users",
                        json={
                            "username": "irrelevant",
                            "password": _STRONG_PASSWORD,
                            "role": "normal_user",
                        },
                        headers={"Authorization": f"Bearer {_API_KEY_TOKEN}"},
                        cookies={"cidx_session": _SESSION_KEY},
                    )
    finally:
        manager_cls.touch_atomic_for_user = original_touch_atomic_for_user

    assert resp.status_code == 201, (
        "Mutation check failed: an unbound touch_atomic() lookup should "
        f"grant this request, but got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Fail-closed fallback: _check_session_window() with no explicit username
# and nothing resolvable from request.state.
# ---------------------------------------------------------------------------


def test_check_session_window_denies_when_no_username_resolvable(manager, user_a):
    """When no username is passed explicitly and request.state carries no
    elevation_username, _check_session_window() must deny with
    elevation_required -- never fall through to an unqualified lookup.

    Uses a plain types.SimpleNamespace for request.state, not MagicMock: a
    MagicMock auto-vivifies any attribute access as a truthy Mock object,
    which would silently defeat this exact fail-closed check by making
    getattr(state, "elevation_username", None) return a Mock (truthy)
    instead of the real, absent value.
    """
    manager.create(_SESSION_KEY, _USER_A, _IP)
    state = types.SimpleNamespace(user_jti=_SESSION_KEY)  # elevation_username absent
    request = types.SimpleNamespace(state=state, cookies={})

    with _elevation_ctx(enforcement=True):
        with pytest.raises(HTTPException) as exc_info:
            _deps._check_session_window(request, "full", manager)  # type: ignore[arg-type]
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]
