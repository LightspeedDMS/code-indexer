"""
Discriminating tests for the self-service MCP-credential elevation gate.

The two SELF-SERVICE MCP-credential mutation routes in `inline_mcp_creds.py`
(`POST /api/mcp-credentials` create, `DELETE /api/mcp-credentials/{id}`
delete -- of the CALLER'S OWN credential) require an active TOTP elevation
window, matching the MCP twins (`mcp/handlers/admin/mcp_credentials.py::
_create_self`/`_delete_self`), which correctly require
`@require_mcp_elevation()`.

The four ADMIN routes in the SAME file (targeting a DIFFERENT user's
credential) are covered separately, in a sibling test module.

Critical distinction from the admin routes' gate: self-service MCP-credential
management is available to EVERY authenticated user, not just admins (see
`web/routes.py::user_mcp_credentials_page`: "any authenticated user can
manage their own MCP credentials"). `dependencies.require_elevation()`
cannot be reused verbatim here -- its `_check` hard-depends on
`get_current_admin_user_hybrid`, which rejects non-admin callers outright
and would break self-service for every NORMAL_USER account. The gate
instead requires TOTP elevation for ANY authenticated user, matching the
MCP twins' `@require_mcp_elevation()` semantics (which check the CALLER's
own TOTP/elevation state, not their role).

These tests exercise the real REST front door (the real
`code_indexer.server.app.app`, `TestClient`) with the real elevation
enforcement + TOTP-setup + session-window logic (only the TOTP service and
MCPCredentialManager are mocked -- never the elevation gate itself).
"""

# Bind the module-level lock directory to the process data dir before
# per-test data dirs are set.
import code_indexer.server.auth.concurrency_protection  # noqa: F401

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole

_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"
_SESSION_KEY = "test-session-jti-mcp-creds-self-service"
_IP = "127.0.0.1"


def _get_app(tmpdir: str):
    """Lazy-import the real app with an isolated DB (mirrors the `_get_app`
    helper pattern used by sibling elevation test files)."""
    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}):
        reset_config_service()
        from code_indexer.server.app import app as _app

        return _app


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def normal_user() -> User:
    return User(
        username="self-service-normal-user",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def admin_user() -> User:
    return User(
        username="self-service-admin-user",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def elevation_manager():
    """Isolated temp-file-backed ElevatedSessionManager (never the real
    shared ~/.cidx-server/elevated_sessions.db)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "elevated_sessions.db")
        yield ElevatedSessionManager(
            idle_timeout_seconds=300, max_age_seconds=1800, db_path=db_path
        )


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """Backstop for _self_service_client's override cleanup: unconditionally
    clear app.dependency_overrides after every test in this file, even if a
    test fails before its generator-based cleanup runs.

    dependencies.elevated_session_manager is restored via
    monkeypatch.setattr at each call site below (never a hand-rolled
    capture-at-fixture-setup fixture): that pattern captures the value
    BEFORE the first _get_app() call in the whole test session has run
    create_app(), and restoring it afterward would overwrite whatever the
    real app wiring produced.
    """
    yield
    from code_indexer.server.app import app as _app

    _app.dependency_overrides.clear()


def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    return (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    )


def _self_service_client(app, user):
    app.dependency_overrides[_deps.get_current_user_web_or_api] = lambda: user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(_deps.get_current_user_web_or_api, None)


def _fake_mcp_credential_manager():
    instance = MagicMock()
    instance.generate_credential_audited.return_value = {
        "client_id": "mcp_client_self_abc",
        "client_secret": "mcp_secret_self_xyz",
        "credential_id": "cred-self-123",
        "name": "self-service-cred",
        "created_at": "2025-01-01T00:00:00Z",
    }
    instance.revoke_credential_audited.return_value = True
    return instance


# ---------------------------------------------------------------------------
# Refused without an active elevation window (both roles)
# ---------------------------------------------------------------------------


class TestSelfServiceMcpCredentialElevationRefusal:
    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_create_self_credential_without_elevation_is_refused(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _elevation_ctx(enforcement=True, mfa_enabled=True)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.post(
                    "/api/mcp-credentials",
                    json={"name": "attempted-self-cred"},
                    cookies={"cidx_session": _SESSION_KEY},
                )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"

    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_delete_self_credential_without_elevation_is_refused(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _elevation_ctx(enforcement=True, mfa_enabled=True)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.delete(
                    "/api/mcp-credentials/some-cred-id",
                    cookies={"cidx_session": _SESSION_KEY},
                )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Succeeds with an active elevation window -- proves the fix works for a
# NON-admin caller too (the critical regression this file guards against).
# ---------------------------------------------------------------------------


class TestSelfServiceMcpCredentialElevationSuccess:
    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_create_self_credential_with_elevation_succeeds(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        elevation_manager.create(_SESSION_KEY, user.username, _IP, "full")
        p1, p2 = _elevation_ctx(enforcement=True, mfa_enabled=True)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.post(
                    "/api/mcp-credentials",
                    json={"name": "elevated-self-cred"},
                    cookies={"cidx_session": _SESSION_KEY},
                )

        assert response.status_code == 201, response.text
        assert response.json()["client_id"] == "mcp_client_self_abc"

    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_delete_self_credential_with_elevation_succeeds(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        elevation_manager.create(_SESSION_KEY, user.username, _IP, "full")
        p1, p2 = _elevation_ctx(enforcement=True, mfa_enabled=True)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.delete(
                    "/api/mcp-credentials/some-cred-id",
                    cookies={"cidx_session": _SESSION_KEY},
                )

        assert response.status_code == 200, response.text
        assert response.json()["message"] == "MCP credential deleted successfully"


# ---------------------------------------------------------------------------
# Scope boundary: the self-service LIST route is read-only and explicitly
# out of scope for the mutation gate above -- it must remain reachable with
# no elevation window at all.
# ---------------------------------------------------------------------------


class TestSelfServiceMcpCredentialListRemainsUnelevated:
    def test_list_self_credentials_requires_no_elevation(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _elevation_ctx(enforcement=True, mfa_enabled=True)

        # No elevation window created at all -- the route must not even
        # attempt the elevation gate. The real handler runs against the
        # isolated (empty) tmpdir SQLite DB, so no mocking is needed.
        for client in _self_service_client(app, normal_user):
            with p1, p2:
                response = client.get(
                    "/api/mcp-credentials",
                    cookies={"cidx_session": _SESSION_KEY},
                )

        # 200 (or the real handler's natural response) -- must NOT be the
        # 403 elevation_required shape the mutating routes now return.
        assert response.status_code != 403 or (
            response.json().get("detail", {}).get("error") != "elevation_required"
        )
