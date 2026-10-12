"""POST /api/admin/metadata-outbox/abandon (content seam step 5a).

Authorization runs through the REAL dependency chain -- the session-cookie
branch of get_current_admin_user_hybrid and the real require_elevation() --
with only its two collaborators stood in (the web session store and the user
store), so the role check and the elevation-window check that refuse a caller
are the production ones:

* a non-admin is refused (403) and nothing is abandoned;
* an admin with no elevation window is refused (403 elevation_required);
* an elevated admin abandons: rows deleted, sweeper stopped, keys returned.

The endpoint drives a real sweeper over a real SQLite registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.control_plane_metadata import ControlPlaneMetadata
from code_indexer.server.auth import dependencies as deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.metadata_outbox.sinks import LocalFilesSink
from code_indexer.server.services.metadata_outbox.sweeper import (
    MetadataOutboxSweeper,
    OutboxSettings,
)
from tests.utils.control_plane_metadata_fixtures import (
    build_schema_template,
    scoped_install,
    sqlite_backend_from_template,
)

ENDPOINT = "/api/admin/metadata-outbox/abandon"
SESSION_KEY = "web-session-for-outbox-test"


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name,
        password_hash="hashed",
        role=role,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_schema_template(tmp_path_factory.mktemp("schema"))


@pytest.fixture
def setup(tmp_path: Path, schema_template: Path) -> Iterator[SimpleNamespace]:
    backend = sqlite_backend_from_template(schema_template, tmp_path)
    golden_repos_dir = tmp_path / "golden-repos"
    metadata = ControlPlaneMetadata(golden_repos_dir, backend=backend)
    metadata.import_files()
    with scoped_install(metadata):
        AliasManager(golden_repos_dir / "aliases").create_alias(
            "example-repo-global", "/snapshots/v_1"
        )
        sweeper = MetadataOutboxSweeper(
            metadata=metadata,
            backend=backend,
            settings=lambda: OutboxSettings(sink="local-files"),
            sink_factory=lambda name: LocalFilesSink(),
        )
        from code_indexer.server.routers.metadata_outbox_admin import router

        app = FastAPI()
        app.include_router(router)
        app.state.metadata_outbox_sweeper = sweeper
        yield SimpleNamespace(app=app, backend=backend, sweeper=sweeper)


@pytest.fixture(autouse=True)
def _restore_auth_globals() -> Iterator[None]:
    saved_users = deps.user_manager
    saved_elevation = deps.elevated_session_manager
    yield
    deps.user_manager = saved_users
    deps.elevated_session_manager = saved_elevation


def _post_as(
    app: FastAPI, user: User, elevation: Optional[ElevatedSessionManager]
) -> Response:
    """POST with a web-session cookie resolving to *user* (from the user
    store, as production does), elevation enforcement ON."""
    users = MagicMock()
    users.get_user.return_value = user
    deps.user_manager = users
    deps.elevated_session_manager = elevation or ElevatedSessionManager(
        idle_timeout_seconds=300, max_age_seconds=1800
    )
    sessions = MagicMock()
    sessions.get_session.return_value = SimpleNamespace(
        username=user.username, role=user.role.value
    )
    totp = MagicMock()
    totp.is_mfa_enabled.return_value = True
    with (
        patch(
            "code_indexer.server.web.auth.get_session_manager", return_value=sessions
        ),
        patch(
            "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled",
            return_value=True,
        ),
        patch("code_indexer.server.web.mfa_routes.get_totp_service", return_value=totp),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        return client.post(ENDPOINT, cookies={"session": SESSION_KEY})


def test_non_admin_is_refused_and_nothing_is_abandoned(setup):
    response = _post_as(setup.app, _user("someone", UserRole.NORMAL_USER), None)

    assert response.status_code == 403
    assert setup.backend.outbox_depth() == 1
    assert setup.backend.get_outbox_state().stopped_at is None


def test_admin_without_elevation_is_refused(setup):
    response = _post_as(setup.app, _user("admin", UserRole.ADMIN), None)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "elevation_required"
    assert setup.backend.outbox_depth() == 1


def test_elevated_admin_abandons_and_stops_the_sweeper(setup):
    elevation = ElevatedSessionManager(idle_timeout_seconds=300, max_age_seconds=1800)
    elevation.create(SESSION_KEY, "admin", "127.0.0.1")

    response = _post_as(setup.app, _user("admin", UserRole.ADMIN), elevation)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "abandoned": [{"kind": "alias", "name": "example-repo-global"}],
        "count": 1,
    }
    assert setup.backend.outbox_depth() == 0
    assert setup.backend.get_outbox_state().stopped_reason == "abandoned"


def test_no_sweeper_is_503(setup):
    setup.app.state.metadata_outbox_sweeper = None
    elevation = ElevatedSessionManager(idle_timeout_seconds=300, max_age_seconds=1800)
    elevation.create(SESSION_KEY, "admin", "127.0.0.1")

    response = _post_as(setup.app, _user("admin", UserRole.ADMIN), elevation)

    assert response.status_code == 503


def test_router_is_mounted_on_the_real_app():
    from code_indexer.server.routers import inline_routes

    source = Path(inline_routes.__file__).read_text()
    assert "app.include_router(metadata_outbox_admin_router)" in source
