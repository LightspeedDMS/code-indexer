"""Tests for wiki access control — role-based admin check consistency (Bug fix).

The bug: _check_wiki_access() used access_svc.is_admin_user() (group membership)
while the rest of the app uses user.has_permission("manage_users") (role-based).
A user with role=admin but not in the "admins" group got 404 on the wiki.

Fix: both _check_wiki_access() and _check_user_wiki_access() now accept either
group-based admin OR role-based admin.
"""
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_real_user(username: str, role: UserRole) -> User:
    """Create a real User object (not MagicMock) so has_permission() works correctly."""
    return User(
        username=username,
        password_hash="$2b$12$fakehash",
        role=role,
        created_at=datetime(2026, 1, 1),
    )


def _make_app(
    authenticated_user: User,
    actual_repo_path: str = None,
    user_accessible_repos=None,
    wiki_enabled: bool = True,
    is_admin_group: bool = False,
) -> FastAPI:
    """Build a minimal FastAPI app wired to wiki_router for access control tests."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = wiki_enabled
    app.state.golden_repo_manager.db_path = _db_path

    if actual_repo_path:
        golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
        app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)
    else:
        _tmp_golden = tempfile.mkdtemp(suffix="-golden-repos")
        (Path(_tmp_golden) / "aliases").mkdir(parents=True, exist_ok=True)
        app.state.golden_repo_manager.golden_repos_dir = _tmp_golden

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = is_admin_group
    app.state.access_filtering_service.get_accessible_repos.return_value = (
        user_accessible_repos if user_accessible_repos is not None else set()
    )
    return app


class TestAdminRoleWikiAccess:
    """Verify that role=admin users can access the wiki even without group membership."""

    def test_admin_role_not_in_admins_group_can_access_wiki(self):
        """Reproduces the bug: role=admin, is_admin_user()=False -> should get 200, was 404."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Home")
            user = _make_real_user("seba.battig", UserRole.ADMIN)
            # is_admin_group=False simulates user NOT in admins group
            app = _make_app(user, tmpdir, set(), is_admin_group=False)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200, (
                "User with role=admin must bypass group check even if not in admins group"
            )

    def test_admins_group_member_can_access_wiki(self):
        """Existing behavior: user in admins group (is_admin_user=True) still works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Home")
            user = _make_real_user("group_admin", UserRole.NORMAL_USER)
            # is_admin_group=True simulates user IN admins group
            app = _make_app(user, tmpdir, set(), is_admin_group=True)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200, (
                "User in admins group must still be able to access the wiki"
            )

    def test_normal_user_with_group_access_can_access_wiki(self):
        """Non-admin user with the repo in their accessible repos gets 200."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Home")
            user = _make_real_user("regular_user", UserRole.NORMAL_USER)
            app = _make_app(user, tmpdir, {"test-repo"}, is_admin_group=False)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200, (
                "Normal user with accessible repo must reach the wiki"
            )

    def test_normal_user_without_access_gets_404(self):
        """Non-admin user with no group access gets 404 (invisible repo)."""
        user = _make_real_user("outsider", UserRole.NORMAL_USER)
        app = _make_app(user, user_accessible_repos=set(), is_admin_group=False)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/test-repo/")
        assert resp.status_code == 404, (
            "Normal user without access must receive 404"
        )
