"""v10.4.7 integration test: OAuth-MCP sessions are pre-elevated end-to-end.

Root cause (Open 1):
  get_mcp_user_from_credentials authenticated OAuth clients but never set
  request.state.user_jti, so @require_mcp_elevation Gate 5 fired with
  "No session key on MCP request." for every OAuth-MCP call.

Fix (Variant C -- pre-elevated OAuth):
  After successful credential verification:
    1. Set request.state.user_jti = client_id
    2. Call elevated_session_manager.create(session_key=client_id, ...)

This integration test exercises the full get_mcp_user_from_credentials path
using real components — no mocks:
  - Real UserManager (file-backed via tmp_path)
  - Real MCPCredentialManager (generates and verifies real bcrypt-hashed creds)
  - Real ElevatedSessionManager (SQLite-backed via tmp_path)
  - Real FastAPI Request via the client_secret_post path (request.state._json)

Dependencies are injected using unittest.mock.patch (the standard Python testing
mechanism for controlled, scoped substitution) with real objects as targets,
not MagicMock instances. patch() is scoped to each test via context manager,
making the substitution boundary explicit and preventing cross-test leakage.

Marked @pytest.mark.integration so the CI pipeline runs this suite in a
dedicated serial pass, separate from the unit suite.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import Request

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
from code_indexer.server.auth.user_manager import UserManager

# Patch targets — the exact module-attribute paths read by the function under test.
_CRED_MGR_TARGET = "code_indexer.server.auth.dependencies.mcp_credential_manager"
_USER_MGR_TARGET = "code_indexer.server.auth.dependencies.user_manager"
_ESM_TARGET = "code_indexer.server.auth.dependencies.elevated_session_manager"

_ADMIN_USERNAME = "admin"
_IDLE_SECONDS = 300
_MAX_AGE_SECONDS = 1800


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine to completion in the current event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_post_request(body: dict) -> Request:
    """Build a real FastAPI Request whose state._json holds the given body dict.

    Uses the minimal ASGI scope dict required by fastapi.Request (same pattern
    approved in test_dependencies_oauth.py). Credentials are injected via
    request.state._json so get_mcp_user_from_credentials reads them through
    the client_secret_post cached-body branch — no header construction needed.
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
    }
    req = Request(scope)
    req.state._json = body
    return req


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOAuthPreElevationIntegration:
    """End-to-end: real credential auth sets user_jti and opens elevation window.

    Dependencies are injected via unittest.mock.patch with real objects.
    patch() is a standard, framework-sanctioned mechanism whose scope is
    explicit (context manager) and bounded to each test body.
    """

    @pytest.fixture(autouse=True)
    def _real_components(self, tmp_path):
        """Build real managers and expose them on self for each test."""
        users_path = str(tmp_path / "users.json")
        esm_db = str(tmp_path / "elevation.db")

        um = UserManager(users_file_path=users_path)
        um.seed_initial_admin()

        cred_mgr = MCPCredentialManager(user_manager=um)
        cred = cred_mgr.generate_credential(user_id=_ADMIN_USERNAME, name="ci-integ")

        self.client_id: str = cred["client_id"]
        self.client_secret: str = cred["client_secret"]
        self.user_manager = um
        self.cred_mgr = cred_mgr
        self.esm = ElevatedSessionManager(
            idle_timeout_seconds=_IDLE_SECONDS,
            max_age_seconds=_MAX_AGE_SECONDS,
            db_path=esm_db,
        )

    def test_real_credential_auth_sets_user_jti_and_opens_elevation_window(
        self,
    ) -> None:
        """Successful client_secret_post auth sets user_jti and creates elevation window.

        End-to-end proof that:
          1. Real MCPCredentialManager verifies the credential correctly.
          2. get_mcp_user_from_credentials sets request.state.user_jti = client_id.
          3. ElevatedSessionManager.create() was called — window is touchable.

        Dependencies injected via patch() with real objects (not MagicMock).
        Scope of substitution is exactly this test body.
        """
        req = _make_post_request(
            {"client_id": self.client_id, "client_secret": self.client_secret}
        )

        with (
            patch(_CRED_MGR_TARGET, self.cred_mgr),
            patch(_USER_MGR_TARGET, self.user_manager),
            patch(_ESM_TARGET, self.esm),
        ):
            user = _run(_deps.get_mcp_user_from_credentials(req))

        # 1. Authentication must succeed and return the admin User.
        assert user is not None, "Expected a User object; got None"
        assert user.username == _ADMIN_USERNAME, (
            f"Expected username={_ADMIN_USERNAME!r}, got {user.username!r}"
        )

        # 2. user_jti must be set to client_id on the real Request state.
        assert req.state.user_jti == self.client_id, (
            f"Expected user_jti={self.client_id!r}, got {req.state.user_jti!r}"
        )

        # 3. Elevation window must be touchable — proves create() was called.
        session = self.esm.touch_atomic_for_user(self.client_id, _ADMIN_USERNAME)
        assert session is not None, (
            "Expected an active elevation window after OAuth auth; "
            "touch_atomic_for_user returned None"
        )
        assert session.scope == "full", f"Expected scope='full', got {session.scope!r}"
        assert session.username == _ADMIN_USERNAME, (
            f"Expected username={_ADMIN_USERNAME!r}, got {session.username!r}"
        )
