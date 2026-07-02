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


@pytest.fixture
def anon_client():
    """TestClient without any auth override (unauthenticated)."""
    yield TestClient(app, raise_server_exceptions=False)
