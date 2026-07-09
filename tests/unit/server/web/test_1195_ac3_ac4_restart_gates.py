"""Story #1195 AC3 / AC4: /restart cluster-aware + elevation gate.

AC3 — verify /restart cluster branch: bumps generation, does NOT call
       _schedule_delayed_restart synchronously (FIX-2); solo branch retains
       _schedule_delayed_restart AFTER the cluster block returns.

       NOTE: The #1200 source guards (cluster calls bump, solo calls schedule)
       already exist in test_launch_snapshot_and_routes_1200.py.
       This file adds the FIX-2 guard (cluster does NOT call schedule).

AC4 — both POST /config/{section} and POST /restart carry require_elevation()
       in their route decorator dependencies (NOT duplicated inside the function
       body). Verified by route inspection + source guards + behavioral tests.

       TestAC4ElevationGateBehavioral: real TestClient + real login confirms:
         - non-elevated admin POSTing to /config/{section} receives 403
           with elevation_required detail
         - elevated admin (bypass_elevation) POSTing to /config/{section}
           receives 200 (not rejected by the gate)
"""

from __future__ import annotations

import re
import secrets
import string
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROUTES_PATH = _REPO_ROOT / "src" / "code_indexer" / "server" / "web" / "routes.py"

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"
_TOKEN_USERNAME_BYTES = 8
_TEST_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Shared helpers for behavioral tests
# ---------------------------------------------------------------------------


def _make_test_password() -> str:
    from code_indexer.server.auth.password_strength_validator import (
        PasswordStrengthValidator,
    )

    validator = PasswordStrengthValidator()
    specials = "!@#%^&*"
    alphabet = string.ascii_letters + string.digits + specials
    for _ in range(10):
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(specials),
        ] + [secrets.choice(alphabet) for _ in range(16)]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        ok, _ = validator.validate(candidate, username="testuser")
        if ok:
            return candidate
    raise AssertionError("_make_test_password() exhausted all attempts")


def _scrape_csrf_token(html: str) -> str:
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "CSRF token not found in HTML"
    return match.group(1)


def _bypass_elevation(app, router) -> None:
    """Override all require_elevation deps — simulates active TOTP window.

    Established pattern from test_users_elevation_gate.py,
    test_groups_toggle_ajax.py, test_dep_map_health_repair_api.py.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


# ---------------------------------------------------------------------------
# AC3: source guards (complement to #1200, focused on FIX-2)
# ---------------------------------------------------------------------------


class TestAC3RestartSourceGuards:
    """FIX-2: cluster branch must NOT synchronously call _schedule_delayed_restart."""

    def _restart_fn_body(self) -> str:
        source = _ROUTES_PATH.read_text()
        fn_start = source.find("def restart_server(")
        assert fn_start != -1
        return source[fn_start : fn_start + 3500]

    def test_cluster_block_does_not_schedule_delayed_restart(self) -> None:
        """FIX-2: _schedule_delayed_restart must NOT appear in the cluster branch."""
        fn_body = self._restart_fn_body()
        bump_pos = fn_body.find("bump_launch_restart_generation")
        assert bump_pos != -1, "bump_launch_restart_generation not found"
        first_return_after_bump = fn_body.find("return", bump_pos)
        assert first_return_after_bump != -1
        cluster_block = fn_body[bump_pos:first_return_after_bump]
        assert "_schedule_delayed_restart" not in cluster_block, (
            "FIX-2: cluster branch must NOT synchronously call _schedule_delayed_restart "
            "— each node signals itself via its own poll loop"
        )

    def test_solo_block_calls_schedule_delayed_restart(self) -> None:
        """Solo path must call _schedule_delayed_restart after the cluster block."""
        fn_body = self._restart_fn_body()
        bump_pos = fn_body.find("bump_launch_restart_generation")
        assert bump_pos != -1
        first_return = fn_body.find("return", bump_pos)
        solo_section = fn_body[first_return:]
        assert "_schedule_delayed_restart" in solo_section, (
            "AC3: solo path must call _schedule_delayed_restart after the cluster return"
        )

    def test_cluster_detection_checks_pool(self) -> None:
        """Cluster detection must use the _pool attribute."""
        fn_body = self._restart_fn_body()
        assert "_pool" in fn_body, (
            "AC3: restart_server must detect cluster via _pool attribute"
        )


# ---------------------------------------------------------------------------
# AC4: elevation gate — decorator-level, not duplicated inside function body
# ---------------------------------------------------------------------------


class TestAC4ElevationGateSource:
    """AC4: require_elevation() in route decorator, not duplicated in function body."""

    def test_config_section_route_decorator_has_require_elevation(self) -> None:
        """POST /config/{section} decorator must have require_elevation()."""
        src = _ROUTES_PATH.read_text()
        route_pos = src.find('"/config/{section}"')
        assert route_pos != -1, '"/config/{section}" not found'
        context = src[max(0, route_pos - 100) : route_pos + 300]
        assert "require_elevation" in context, (
            "AC4: POST /config/{section} route must have require_elevation() in decorator"
        )

    def test_restart_route_decorator_has_require_elevation(self) -> None:
        """POST /restart decorator must have require_elevation()."""
        src = _ROUTES_PATH.read_text()
        route_pos = src.find('"/restart"')
        assert route_pos != -1, '"/restart" not found'
        context = src[max(0, route_pos - 100) : route_pos + 300]
        assert "require_elevation" in context, (
            "AC4: POST /restart route must have require_elevation() in decorator"
        )

    def test_no_duplicate_elevation_inside_update_config_body(self) -> None:
        """AC4: require_elevation must NOT be duplicated inside update_config_section body."""
        src = _ROUTES_PATH.read_text()
        fn_start = src.find("async def update_config_section(")
        assert fn_start != -1
        fn_body = src[fn_start : fn_start + 8000]
        count = fn_body.count("require_elevation")
        assert count == 0, (
            f"AC4: update_config_section body must NOT reference require_elevation "
            f"({count} occurrences found). Gate belongs in decorator only."
        )

    def test_no_duplicate_elevation_inside_restart_server_body(self) -> None:
        """AC4: require_elevation must NOT be duplicated inside restart_server body."""
        src = _ROUTES_PATH.read_text()
        fn_start = src.find("def restart_server(")
        assert fn_start != -1
        fn_body = src[fn_start : fn_start + 3500]
        count = fn_body.count("require_elevation")
        assert count == 0, (
            f"AC4: restart_server body must NOT reference require_elevation "
            f"({count} occurrences found). Gate belongs in decorator only."
        )


class TestAC4ElevationGateRouteInspection:
    """AC4: FastAPI route objects must carry the require_elevation dependency."""

    _ELEVATION_QUALNAME = "require_elevation.<locals>._check"

    def _get_web_router_routes(self):
        from code_indexer.server.web.routes import web_router

        return list(web_router.routes)

    def _route_has_elevation_dep(self, route) -> bool:
        for dep in getattr(route, "dependencies", []):
            dep_callable = getattr(dep, "dependency", None)
            if dep_callable is None:
                continue
            if getattr(dep_callable, "__qualname__", "") == self._ELEVATION_QUALNAME:
                return True
        return False

    def test_config_section_route_has_elevation_dependency(self) -> None:
        """FastAPI route object for POST /config/{section} must carry require_elevation()."""
        from fastapi.routing import APIRoute

        routes = self._get_web_router_routes()
        config_route = next(
            (
                r
                for r in routes
                if isinstance(r, APIRoute)
                and r.path == "/config/{section}"
                and "POST" in r.methods
            ),
            None,
        )
        assert config_route is not None, "POST /config/{section} route not found"
        assert self._route_has_elevation_dep(config_route), (
            "AC4: POST /config/{section} FastAPI route must have require_elevation() "
            "wired in its dependencies list"
        )

    def test_restart_route_has_elevation_dependency(self) -> None:
        """FastAPI route object for POST /restart must carry require_elevation()."""
        from fastapi.routing import APIRoute

        routes = self._get_web_router_routes()
        restart_route = next(
            (
                r
                for r in routes
                if isinstance(r, APIRoute)
                and r.path == "/restart"
                and "POST" in r.methods
            ),
            None,
        )
        assert restart_route is not None, "POST /restart route not found"
        assert self._route_has_elevation_dep(restart_route), (
            "AC4: POST /restart FastAPI route must have require_elevation() "
            "wired in its dependencies list"
        )


# ---------------------------------------------------------------------------
# AC4: behavioral elevation gate — real TestClient + real login
# ---------------------------------------------------------------------------


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app_with_db(tmpdir_path):
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(tmpdir_path / "test.db")).initialize_database()
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        app = create_app()
        original_overrides = dict(app.dependency_overrides)
        yield app
        app.dependency_overrides = original_overrides
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    with TestClient(app_with_db) as c:
        yield c


@pytest.fixture
def admin_session(client, tmpdir_path):
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    um = UserManager(
        use_sqlite=True, db_path=str(tmpdir_path / "data" / "cidx_server.db")
    )
    username = secrets.token_hex(_TOKEN_USERNAME_BYTES)
    password = _make_test_password()
    um.create_user(username=username, password=password, role=UserRole.ADMIN)

    resp = client.get("/login")
    csrf = _scrape_csrf_token(resp.text)
    login = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies=resp.cookies,
        follow_redirects=False,
    )
    assert login.status_code == 303, f"Login failed: {login.status_code}"
    for name, val in login.cookies.items():
        client.cookies.set(name, val)
    return login.cookies


@pytest.fixture
def config_csrf(client, admin_session):
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    return _scrape_csrf_token(resp.text)


_ENFORCEMENT_PATCH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestAC4ElevationGateBehavioral:
    """AC4 (FIX 2): behavioral proof that require_elevation() gate fires on the route.

    Uses real TestClient + real login — no source-grep or decorator introspection.
    Covers the two critical paths:
      1. Non-elevated admin with enforcement ON → 403 elevation_required.
      2. Elevated admin (bypass_elevation) → 200 (gate passes, not rejected).

    The require_elevation kill switch (_is_elevation_enforcement_enabled) defaults
    to False in test environments, so we patch it to True for the non-elevated test.
    The elevated test overrides the dependency directly via _bypass_elevation, so
    enforcement state is irrelevant there.
    """

    def test_non_elevated_admin_gets_403_elevation_gate(
        self, client, admin_session, config_csrf
    ) -> None:
        """AC4: POST /config/server without elevation (enforcement ON) → 403.

        The gate raises either 'totp_setup_required' (when the admin has no TOTP
        MFA set up yet, which is always the case in the test DB) or
        'elevation_required' (when TOTP is set up but no active elevation window
        exists).  Both are 403 responses from require_elevation() — either proves
        the gate fires.
        """
        with patch(_ENFORCEMENT_PATCH, return_value=True):
            resp = client.post(
                "/admin/config/server",
                data={
                    "csrf_token": config_csrf,
                    "host": "0.0.0.0",
                    "port": "8000",
                    "workers": "1",
                    "log_level": "INFO",
                },
                cookies=admin_session,
                follow_redirects=False,
            )
        assert resp.status_code == 403, (
            f"AC4: non-elevated admin POST /config/server must return 403, "
            f"got {resp.status_code}. Body: {resp.text[:200]}"
        )
        body = resp.text.lower()
        # Both totp_setup_required and elevation_required are valid gate responses
        assert (
            "totp_setup_required" in body
            or "elevation_required" in body
            or "elevation required" in body
        ), (
            "AC4: 403 response must contain 'totp_setup_required' or 'elevation_required'. "
            f"Got: {resp.text[:300]}"
        )

    def test_elevated_admin_config_post_succeeds(
        self, app_with_db, client, admin_session, config_csrf
    ) -> None:
        """AC4: POST /config/server WITH elevation (bypass) → 200, gate passes.

        _bypass_elevation replaces require_elevation() dependency with a no-op,
        simulating an active TOTP window.  The route must return 200.
        Note: the rendered page includes 'elevation_required' in its JavaScript
        fetch-interceptor code — we check HTTP status, not page text.
        """
        from code_indexer.server.web.routes import web_router

        _bypass_elevation(app_with_db, web_router)

        resp = client.post(
            "/admin/config/server",
            data={
                "csrf_token": config_csrf,
                "host": "0.0.0.0",
                "port": "8000",
                "workers": "1",
                "log_level": "INFO",
            },
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 200, (
            f"AC4: elevated admin POST /config/server must return 200, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )
        # Must be an HTML config page, not a JSON error response
        content_type = resp.headers.get("content-type", "")
        assert "text/html" in content_type, (
            f"AC4: elevated POST must return HTML config page, got {content_type}"
        )
