"""Real-service environment for the repository access-control route tests.

Every service is real: AccessFilteringService over a GroupAccessManager
(temp SQLite), GoldenRepoManager over its SQLite metadata backend with real
one-commit git clones, ActivatedRepoManager submitting to a real
BackgroundJobManager, and the golden repos registered as global repos in
the real global registry. Nothing about the access decision is mocked.

Repository aliases, URLs and usernames are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.shared_operations import GlobalRepoOperations
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.global_routes import routes as global_routes
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
)
from code_indexer.server.routers.inline_repos import register_repo_routes
from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.storage.database_manager import DatabaseSchema

GRANTED_REPO = "example-repo"
UNGRANTED_REPO = "other-repo"
# Granted to the power user's group but not registered as a golden repo, so
# a granted activation request reaches the route's "not found" response.
GHOST_REPO = "retired-repo"
POWER_USERNAME = "example_power_user"
ADMIN_USERNAME = "example_admin"


def repo_url(alias: str) -> str:
    return f"https://git.example.com/example/{alias}.git"


def _init_git_repo(path: Path) -> None:
    """Create a real one-commit git repository on branch 'main' at *path*."""
    path.mkdir(parents=True)
    (path / "README.md").write_text("example\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "README.md"],
        [
            "git",
            "-c",
            "user.name=example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
    ):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)


def _make_user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def power_user() -> User:
    return _make_user(POWER_USERNAME, UserRole.POWER_USER)


def admin() -> User:
    return _make_user(ADMIN_USERNAME, UserRole.ADMIN)


class Env:
    """Real services plus a TestClient bound to the repository routes."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        data_dir = tmp_path / "gr"
        data_dir.mkdir()
        # Real server schema: the global registry and the golden repo
        # metadata share <data_dir>/cidx_server.db, as on a server.
        DatabaseSchema(str(data_dir / "cidx_server.db")).initialize_database()
        self.golden_repo_manager = GoldenRepoManager(data_dir=str(data_dir))
        golden_dir = Path(self.golden_repo_manager.golden_repos_dir)
        self.global_ops = GlobalRepoOperations(str(golden_dir))
        for alias in (GRANTED_REPO, UNGRANTED_REPO):
            clone_path = golden_dir / alias
            _init_git_repo(clone_path)
            self.golden_repo_manager._sqlite_backend.add_repo(
                alias=alias,
                repo_url=repo_url(alias),
                default_branch="main",
                clone_path=str(clone_path),
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            self.global_ops.registry.register_global_repo(
                repo_name=alias,
                alias_name=f"{alias}-global",
                repo_url=repo_url(alias),
                index_path=str(clone_path),
            )
        self.job_manager = BackgroundJobManager(
            storage_path=str(tmp_path / "jobs.json")
        )
        self.activated_repo_manager = ActivatedRepoManager(
            data_dir=str(tmp_path / "activated"),
            golden_repo_manager=self.golden_repo_manager,
            background_job_manager=self.job_manager,
        )
        gam = GroupAccessManager(tmp_path / "groups.db")
        group = gam.create_group("restricted", "test group")
        gam.assign_user_to_group(POWER_USERNAME, group.id, assigned_by="test")
        gam.grant_repo_access(GRANTED_REPO, group.id, granted_by="test")
        gam.grant_repo_access(GHOST_REPO, group.id, granted_by="test")
        admins = gam.get_group_by_name("admins")
        assert admins is not None, "bootstrap must create the 'admins' group"
        gam.assign_user_to_group(ADMIN_USERNAME, admins.id, assigned_by="test")
        self.access_service = AccessFilteringService(gam)

    def client(
        self, user: User, access_service: Optional[AccessFilteringService]
    ) -> TestClient:
        app = FastAPI()
        listing = RepositoryListingManager(
            golden_repo_manager=self.golden_repo_manager,
            activated_repo_manager=self.activated_repo_manager,
        )
        register_repo_routes(
            app,
            activated_repo_manager=self.activated_repo_manager,
            golden_repo_manager=self.golden_repo_manager,
            repository_listing_manager=listing,
            background_job_manager=self.job_manager,
        )
        register_repos_v2_routes(
            app,
            activated_repo_manager=self.activated_repo_manager,
            repository_listing_manager=listing,
            background_job_manager=self.job_manager,
        )
        app.include_router(global_routes.router)
        app.state.access_filtering_service = access_service
        app.dependency_overrides[dependencies.get_current_power_user] = lambda: user
        app.dependency_overrides[dependencies.get_current_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    def submitted_jobs(self) -> List[Dict[str, Any]]:
        return [
            {"job_id": j.job_id, "operation_type": j.operation_type, "user": j.username}
            for j in self.job_manager.jobs.values()
        ]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    global_routes.set_golden_repos_dir(e.golden_repo_manager.golden_repos_dir)
    try:
        yield e
    finally:
        global_routes._golden_repos_dir = None
        e.job_manager.shutdown()
