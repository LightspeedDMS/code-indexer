"""Unit tests for Story #923 AC8: elevation revocation on logout.

Uses the established project test pattern of patching the module-level
elevated_session_manager singleton (same approach as test_elevation_routes.py).
"""

import pytest
from starlette import status
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.web.routes import web_router, user_router

_ESM_PATH = "code_indexer.server.auth.elevated_session_manager.elevated_session_manager"


def _make_logout_client(router, prefix: str) -> TestClient:
    """Build a minimal FastAPI TestClient with the given router mounted at prefix."""
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return TestClient(app, raise_server_exceptions=True, follow_redirects=False)


@pytest.mark.parametrize(
    "router,prefix,path",
    [
        (web_router, "/admin", "/admin/logout"),
        (user_router, "/user", "/user/logout"),
    ],
    ids=["admin_logout", "user_logout"],
)
def test_logout_calls_revoke(router, prefix, path):
    """Logout routes revoke the elevation window for the session cookie."""
    esm = MagicMock()
    fake_sm = MagicMock()
    client = _make_logout_client(router, prefix)
    with (
        patch(_ESM_PATH, esm),
        patch(
            "code_indexer.server.web.routes.get_session_manager",
            return_value=fake_sm,
        ),
    ):
        resp = client.get(path, cookies={"cidx_session": "test-session-key"})

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    esm.revoke.assert_called_once_with("test-session-key")
