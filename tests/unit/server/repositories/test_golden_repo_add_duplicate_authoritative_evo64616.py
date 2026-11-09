"""issue-64616: the add-golden-repo duplicate guard reads the shared registry.

Root cause: ``_submit_add_job`` decided "already exists" with the cache-first
resolver. In a cluster each worker caches golden repos; a removal on ANOTHER
node (a failed add rolls its row back on the node that ran it) never evicts
this worker's entry, so every later registration of that alias was refused as
"already exists" while ``GET /api/admin/golden-repos`` -- which reads the
shared backend -- did not list it. A seeder re-registering it hourly got a 500
every hour, on every pod that had cached the alias.

The fix reads the shared backend authoritatively (evicting the stale entry),
and a genuine duplicate is the typed ``GoldenRepoAlreadyExistsError`` so the
REST layer answers 409, not 500.

A worker's cache going stale is reproduced on a real ``GoldenRepoManager``
over its real SQLite backend: the row is removed from the backend directly,
leaving this worker's ``golden_repos`` entry behind -- exactly what a removal
on another node does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoAlreadyExistsError,
    GoldenRepoError,
    GoldenRepoManager,
)
from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes

_ALIAS = "example-repo"


def _add_row(manager: GoldenRepoManager, alias: str, clone_path: Path) -> None:
    """Register *alias* in the shared backend, as any node's add would."""
    manager._sqlite_backend.add_repo(
        alias=alias,
        repo_url=f"local://{alias}",
        default_branch="main",
        clone_path=str(clone_path),
        created_at="2026-01-01T00:00:00+00:00",
        enable_temporal=False,
        temporal_options=None,
    )


def _cache_then_remove_elsewhere(
    manager: GoldenRepoManager, alias: str, clone_path: Path
) -> None:
    """This worker caches *alias*; another node then removes its row."""
    _add_row(manager, alias, clone_path)
    manager.golden_repos[alias] = GoldenRepo(**manager._sqlite_backend.get_repo(alias))
    manager._sqlite_backend.remove_repo(alias)


@pytest.fixture
def manager(tmp_path: Path) -> GoldenRepoManager:
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    mgr.background_job_manager = Mock()
    mgr.background_job_manager.submit_job = Mock(return_value="job-1")
    return mgr


class TestAddGuardReadsSharedRegistry:
    def test_setup_reproduces_a_stale_cache_hit(self, manager, tmp_path) -> None:
        _cache_then_remove_elsewhere(manager, _ALIAS, tmp_path / "clone")

        assert _ALIAS in manager.golden_repos
        assert manager._sqlite_backend.get_repo(_ALIAS) is None

    def test_alias_removed_on_another_node_can_be_registered_again(
        self, manager, tmp_path
    ) -> None:
        _cache_then_remove_elsewhere(manager, _ALIAS, tmp_path / "clone")

        job_id = manager.add_golden_repo(
            repo_url=f"local://{_ALIAS}",
            alias=_ALIAS,
            submitter_username="example-admin",
        )

        assert job_id == "job-1"
        manager.background_job_manager.submit_job.assert_called_once()
        assert _ALIAS not in manager.golden_repos, "stale cache entry not evicted"

    def test_genuine_duplicate_raises_the_typed_error(self, manager, tmp_path) -> None:
        _add_row(manager, _ALIAS, tmp_path / "clone")

        with pytest.raises(GoldenRepoAlreadyExistsError, match="already exists"):
            manager.add_golden_repo(
                repo_url=f"local://{_ALIAS}",
                alias=_ALIAS,
                submitter_username="example-admin",
            )
        manager.background_job_manager.submit_job.assert_not_called()

    def test_typed_error_is_still_a_golden_repo_error(self) -> None:
        # Callers that catch GoldenRepoError keep working.
        assert issubclass(GoldenRepoAlreadyExistsError, GoldenRepoError)


class TestRegisterLocalRepoReadsSharedRegistry:
    def test_alias_removed_on_another_node_is_registered_again(
        self, manager, tmp_path
    ) -> None:
        folder = tmp_path / "local-folder"
        folder.mkdir()
        _cache_then_remove_elsewhere(manager, _ALIAS, folder)

        registered = manager.register_local_repo(
            _ALIAS, folder, fire_lifecycle_hooks=False
        )

        assert registered is True
        assert manager._sqlite_backend.get_repo(_ALIAS) is not None

    def test_registered_alias_stays_idempotent(self, manager, tmp_path) -> None:
        folder = tmp_path / "local-folder"
        folder.mkdir()
        _add_row(manager, _ALIAS, folder)

        assert (
            manager.register_local_repo(_ALIAS, folder, fire_lifecycle_hooks=False)
            is False
        )


def _admin() -> User:
    return User(
        username="example-admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _client(exc: Exception) -> TestClient:
    app = FastAPI()
    grm = MagicMock()
    grm.add_golden_repo.side_effect = exc
    register_admin_ops_routes(
        app,
        jwt_manager=MagicMock(),
        user_manager=MagicMock(),
        golden_repo_manager=grm,
        background_job_manager=MagicMock(),
        config_service=MagicMock(),
        server_config=MagicMock(),
        data_dir="/tmp",
        job_tracker=MagicMock(),
    )
    admin = _admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    return TestClient(app, raise_server_exceptions=False)


_BODY = {"repo_url": "https://example.com/repo.git", "alias": _ALIAS}


class TestAddEndpointStatus:
    def test_existing_alias_is_409_with_detail(self) -> None:
        exc = GoldenRepoAlreadyExistsError(
            f"Golden repository alias '{_ALIAS}' already exists"
        )

        resp = _client(exc).post("/api/admin/golden-repos", json=_BODY)

        assert resp.status_code == 409, resp.text
        assert "already exists" in resp.text

    def test_other_golden_repo_error_is_still_500(self) -> None:
        exc = GoldenRepoError("stale global registry entry")

        resp = _client(exc).post("/api/admin/golden-repos", json=_BODY)

        assert resp.status_code == 500
