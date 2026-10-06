"""
Shared helpers and fixtures for inline route coverage tests.

Provides:
- _patch_closure(): mutate closure cell of a route handler temporarily
- _find_route_handler(): look up a registered route endpoint by path+method
- _make_admin() / _make_regular_user(): User factory helpers
- pytest fixtures: admin_client, user_client, anon_client
"""

import ctypes
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import (
    get_current_user,
    get_current_admin_user,
    get_current_admin_user_hybrid,
)
from code_indexer.server.auth.user_manager import User, UserRole


def _find_elevation_check_dependencies() -> List:
    """Return all require_elevation._check callables registered across app routes.

    Story #925 added @require_elevation() to several admin routes. Tests that
    verify schema/handler logic (not elevation gating) need to bypass the
    elevation check dependency entirely. FastAPI resolves Depends() at route
    registration time, so we scan all routes for _check closures and return
    them so admin_client can override them in app.dependency_overrides.
    """
    deps = []
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            fn = dep.call
            qualname = getattr(fn, "__qualname__", "")
            if "require_elevation.<locals>._check" in qualname:
                deps.append(fn)
    return deps


# ---------------------------------------------------------------------------
# User factory helpers
# ---------------------------------------------------------------------------


def _make_admin() -> User:
    return User(
        username="testadmin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_regular_user() -> User:
    return User(
        username="testuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Route lookup helper
# ---------------------------------------------------------------------------


def _find_route_handler(path: str, method: str):
    """Return the registered endpoint function for a given path + HTTP method."""
    for route in app.routes:
        if (
            hasattr(route, "path")
            and route.path == path
            and hasattr(route, "methods")
            and method.upper() in route.methods
        ):
            return route.endpoint
    raise KeyError(f"Route not found: {method} {path}")


# ---------------------------------------------------------------------------
# Closure mutation helper
# ---------------------------------------------------------------------------


@contextmanager
def _patch_closure(handler, var_name: str, replacement):
    """
    Temporarily replace a closure cell in *handler* by name.

    The inline route handlers are closures over real manager instances
    (not module-level globals), so unittest.mock.patch() cannot reach
    them.  We mutate the cell directly via ctypes and restore the
    original value on exit.
    """
    freevars = handler.__code__.co_freevars
    idx = freevars.index(var_name)
    cell = handler.__closure__[idx]
    original = cell.cell_contents
    ctypes.cast(id(cell), ctypes.py_object).value.cell_contents = replacement
    try:
        yield
    finally:
        ctypes.cast(id(cell), ctypes.py_object).value.cell_contents = original


_UNSET = object()


@contextmanager
def _access_service_granting(
    db_path, username: str, repos: List[str], activated_repo_manager=None
):
    """Wire a REAL access service on app.state granting *repos* to *username*.

    Routes that name or list golden repositories require group access and
    fail closed without the access service; the previous app.state value
    is restored on exit. *activated_repo_manager* (the one the route under
    test uses) lets the service judge the caller's activations by their
    source repositories.
    """
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    gam = GroupAccessManager(db_path)
    group = gam.create_group("route-test", "route test group")
    gam.assign_user_to_group(username, group.id, assigned_by="test")
    for repo in repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")
    previous = getattr(app.state, "access_filtering_service", _UNSET)
    app.state.access_filtering_service = AccessFilteringService(
        gam, activated_repo_manager=activated_repo_manager
    )
    try:
        yield
    finally:
        if previous is _UNSET:
            del app.state.access_filtering_service
        else:
            app.state.access_filtering_service = previous


@contextmanager
def _access_service_admin(db_path, *usernames: str):
    """Wire a REAL access service on app.state whose admins group holds
    *usernames*.

    Routes serving the caller's ACTIVATED repository check the caller's
    grants on that activation's source repositories (admins bypass, as on
    MCP). Tests that pin such a route's own behaviour -- with the activated
    repo manager faked -- make their caller an admin of a real group store
    so the guard passes for real. Enter it AFTER a lifespan-running
    TestClient starts (the lifespan installs its own service); the previous
    app.state value is restored on exit.
    """
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    gam = GroupAccessManager(db_path)
    admins = gam.get_group_by_name("admins")
    assert admins is not None, "bootstrap must create the 'admins' group"
    for username in usernames:
        gam.assign_user_to_group(username, admins.id, assigned_by="test")
    previous = getattr(app.state, "access_filtering_service", _UNSET)
    app.state.access_filtering_service = AccessFilteringService(gam)
    try:
        yield
    finally:
        if previous is _UNSET:
            del app.state.access_filtering_service
        else:
            app.state.access_filtering_service = previous


# ---------------------------------------------------------------------------
# Shared pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client():
    """TestClient with admin user bypassing JWT and elevation enforcement.

    Overrides all standard auth dependencies AND every require_elevation._check
    closure found on registered routes so that schema/handler tests are not
    blocked by the elevation gate (Story #925).
    """
    admin = _make_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    for check_dep in _find_elevation_check_dependencies():
        app.dependency_overrides[check_dep] = lambda: admin
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def user_client():
    """TestClient with regular (non-admin) user bypassing JWT."""
    user = _make_regular_user()
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# The caller the activated-repos / indexing router-only tests authenticate as.
ROUTER_TEST_CALLER = "alice"


@pytest.fixture(autouse=True)
def caller_is_access_admin(tmp_path_factory):
    """Active ONLY in a test module that imports it: the helper's regular
    user and admin, and ROUTER_TEST_CALLER, are admins of a real access
    service for each test, so the activated-repo guard passes for routes
    whose own behaviour the module pins. A test that installs its own
    service (e.g. via _access_service_granting) overrides it for its
    duration. Not for lifespan-running clients (the lifespan installs its
    own service)."""
    with _access_service_admin(
        # Its own directory: tests often use tmp_path as a repository root.
        tmp_path_factory.mktemp("access-admin") / "groups.db",
        _make_regular_user().username,
        _make_admin().username,
        ROUTER_TEST_CALLER,
    ):
        yield


@pytest.fixture
def anon_client():
    """TestClient without any auth override (unauthenticated)."""
    yield TestClient(app, raise_server_exceptions=False)
