"""
Shared registry and helpers for the REST API route smoke/regression harness (Story #409).

All domain smoke test files import from here to share:
- COVERED_ROUTES: populated as a side effect of each smoke test
- EXCLUDED_ROUTES: routes legitimately excluded from coverage
- CRASH_INDICATORS: strings that indicate a real server crash in a 500 response
- _assert_no_crash(): assertion helper
- _smoke(): unified smoke call that registers coverage
- Shared pytest fixtures: mock_admin_user, client
"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user

# ---------------------------------------------------------------------------
# Module-level coverage tracking (shared across all smoke test modules)
# ---------------------------------------------------------------------------

COVERED_ROUTES: set[str] = set()

# Routes legitimately excluded — FastAPI built-ins and SSE/protocol endpoints.
EXCLUDED_ROUTES: set[str] = {
    "GET /docs",
    "GET /redoc",
    "GET /openapi.json",
    "GET /docs/oauth2-redirect",
    # DELETE /mcp is the MCP SSE protocol disconnect, not a standard REST DELETE
    "DELETE /mcp",
}

# Strings that identify real server crashes vs expected auth/validation errors.
CRASH_INDICATORS = [
    "NameError",
    "AttributeError",
    "ModuleNotFoundError",
    "ImportError",
    "has no attribute",
    "is not defined",
    "cannot import name",
    "TypeError: 'NoneType'",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_no_crash(response, route_label: str) -> None:
    """Assert the response is not a server crash (500 with crash indicators)."""
    if response.status_code == 500:
        body = response.text
        for indicator in CRASH_INDICATORS:
            assert indicator not in body, (
                f"CRASH detected on {route_label}: "
                f"HTTP 500 containing '{indicator}'.\nResponse body:\n{body[:500]}"
            )


def _smoke(client: TestClient, method: str, path: str, **kwargs) -> None:
    """
    Execute a smoke call: invoke route, assert no crash, register coverage.

    Acceptable responses: anything NOT a 500 with crash indicators.
    401/403/404/422 are acceptable — the route is alive.
    """
    func = getattr(client, method.lower())
    response = func(path, **kwargs)
    _assert_no_crash(response, f"{method} {path}")
    COVERED_ROUTES.add(f"{method} {path}")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_admin_user():
    """Admin-level mock user for dependency override."""
    from code_indexer.server.auth.user_manager import UserRole

    user = Mock()
    user.username = "testadmin"
    user.role = UserRole.ADMIN
    user.email = "testadmin@example.com"
    return user


@pytest.fixture(scope="module")
def client(mock_admin_user):
    """
    TestClient with get_current_user overridden to return a mock admin user.

    Scope is module-level for performance — one client per test file.
    Auth routes that bypass dependency injection are called directly.
    """
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    app.dependency_overrides.clear()
