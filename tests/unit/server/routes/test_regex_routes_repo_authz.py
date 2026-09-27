"""Regression tests: POST /api/regex/search must enforce repo-level
authorization (single-repo AND omni/multi-repo forms).

Front door: real FastAPI TestClient against regex_routes.router, with a REAL
AccessFilteringService backed by a REAL GroupAccessManager (temp SQLite DB) --
not a mock of the access-control decision itself. Only the filesystem/ripgrep
search backend is mocked (as the existing test_regex_routes.py suite already
does), since this test targets the authorization gate, not search behavior.

Repository aliases and usernames below are neutral placeholders
(example-repo-global / other-repo-global / example_user).

Scenarios:
- normal_user without the repo's group grant -> 403, single-repo form
- normal_user without the repo's group grant -> 403, omni/list form (all
  ungranted)
- omni form mixing one granted + one ungranted alias -> 403 (no silent
  partial results -- the granted repo must NOT be searched either)
- admin user bypasses the check entirely -> 200
- normal_user WITH the repo's group grant -> 200, single-repo form
- normal_user WITH both repos' group grant -> 200, omni/list form
- '-global' suffix is normalized before the access check (both directions:
  requesting with the suffix against a bare grant, and requesting the bare
  alias directly)
- denial never reaches repo-path resolution or the search backend
- a nonexistent alias and a real-but-ungranted alias produce identical
  403 responses (existence is never leaked to an unauthorized caller)
- access_filtering_service missing from app.state -> exactly 500 (fails
  closed, never silently permissive)
- the repo-level access check itself runs off the event-loop thread via
  anyio.to_thread.run_sync (this route handler is `async def`; the
  synchronous DB reads inside require_repo_access() must never block the
  event loop directly)
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.routes import regex_routes


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def group_db_path() -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _build_access_service(
    db_path: Path,
    *,
    granted_username: str,
    granted_repos: list,
    admin_username: str = "admin_user",
) -> AccessFilteringService:
    """Build a REAL AccessFilteringService with a REAL GroupAccessManager.

    Creates one custom group holding exactly ``granted_repos``, assigns
    ``granted_username`` to it, and assigns ``admin_username`` to the
    bootstrap 'admins' default group.
    """
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(granted_username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")

    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group(admin_username, admins_group.id, assigned_by="test")

    return AccessFilteringService(gam)


def _build_app(user: User, access_service: AccessFilteringService) -> FastAPI:
    app = FastAPI()
    app.include_router(regex_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.state.access_filtering_service = access_service
    return app


VALID_SINGLE_BODY: dict = {
    "pattern": r"def\s+\w+",
    "repository_alias": "example-repo-global",
}


def _patch_search_backend():
    """Patch repo-path resolution and search execution.

    If the authorization gate is missing/broken, these patches would let
    the request reach real "results". The RED
    test asserts the request never gets this
    far.
    """
    mock_result = MagicMock()
    mock_result.matches = []
    mock_result.total_matches = 0
    mock_result.truncated = False
    mock_result.read_capped = False
    mock_result.search_engine = "ripgrep"
    mock_result.search_time_ms = 1.0
    return (
        patch(
            "code_indexer.server.routes.regex_routes._resolve_repo_path",
            return_value="/some/repo/path",
        ),
        patch(
            "code_indexer.server.routes.regex_routes.RegexSearchService",
            return_value=MagicMock(search=AsyncMock(return_value=mock_result)),
        ),
    )


class TestRegexSearchRepoAuthz:
    def test_single_repo_denied_for_ungranted_user_returns_403(self, group_db_path):
        """normal_user whose group lacks 'example-repo' gets 403, not search
        results, and neither repo-path resolution nor the search backend is
        ever reached."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        p1, p2 = _patch_search_backend()
        with p1 as mock_resolve, p2 as mock_search_cls:
            resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert "example-repo-global" in detail["detail"]
        mock_resolve.assert_not_called()
        mock_search_cls.assert_not_called()

    def test_omni_all_ungranted_returns_403_no_resolution_no_search(
        self, group_db_path
    ):
        """Omni form: user granted neither repo -> 403, no results leaked,
        and neither repo-path resolution nor the search backend is reached."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=[]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        body = {
            "pattern": r"def\s+\w+",
            "repository_alias": ["example-repo-global", "other-repo-global"],
        }
        p1, p2 = _patch_search_backend()
        with p1 as mock_resolve, p2 as mock_search_cls:
            resp = client.post("/api/regex/search", json=body)

        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "access_denied"
        mock_resolve.assert_not_called()
        mock_search_cls.assert_not_called()

    def test_omni_mixed_granted_and_ungranted_returns_403_no_partial_results(
        self, group_db_path
    ):
        """Omni form mixing one granted + one ungranted alias must be
        rejected wholesale -- not silently narrowed to the granted repo."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        body = {
            "pattern": r"def\s+\w+",
            "repository_alias": ["example-repo-global", "other-repo-global"],
        }
        p1, p2 = _patch_search_backend()
        with p1 as mock_resolve, p2 as mock_search_cls:
            resp = client.post("/api/regex/search", json=body)

        assert resp.status_code == 403
        # No partial results field leaking the granted repo's content.
        assert "matches" not in resp.json()
        mock_resolve.assert_not_called()
        mock_search_cls.assert_not_called()

    def test_admin_bypasses_check(self, group_db_path):
        """Admin user is not subject to the group-access check."""
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin, access_service)
        client = TestClient(app)

        p1, p2 = _patch_search_backend()
        with p1, p2:
            resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 200

    def test_granted_user_succeeds_single_repo(self, group_db_path):
        """User whose group IS granted the repo gets a normal 200 response."""
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        p1, p2 = _patch_search_backend()
        with p1, p2:
            resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 200

    def test_granted_user_succeeds_omni_all_granted(self, group_db_path):
        """Omni form: user granted BOTH requested repos gets a normal 200
        response (all-granted success case for the multi-repo form)."""
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo", "other-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        body = {
            "pattern": r"def\s+\w+",
            "repository_alias": ["example-repo-global", "other-repo-global"],
        }
        p1, p2 = _patch_search_backend()
        with p1, p2:
            resp = client.post("/api/regex/search", json=body)

        assert resp.status_code == 200

    def test_granted_user_succeeds_bare_alias_without_global_suffix(
        self, group_db_path
    ):
        """Reverse of the '-global' normalization case: requesting the BARE
        alias directly (no '-global' suffix) against a bare grant must also
        succeed -- normalization must not be a one-way requirement."""
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        body = {"pattern": r"def\s+\w+", "repository_alias": "example-repo"}
        p1, p2 = _patch_search_backend()
        with p1, p2:
            resp = client.post("/api/regex/search", json=body)

        assert resp.status_code == 200

    def test_nonexistent_alias_and_ungranted_alias_get_identical_denial(
        self, group_db_path
    ):
        """A caller lacking access must not be able to distinguish "this
        repo doesn't exist" from "this repo exists but you're not granted
        it" -- the access check runs BEFORE any existence/resolution check,
        so both cases must produce the identical 403 envelope shape."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=[]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        p1, p2 = _patch_search_backend()

        with p1, p2:
            resp_real = client.post(
                "/api/regex/search",
                json={
                    "pattern": r"def\s+\w+",
                    "repository_alias": "other-repo-global",
                },
            )
        with p1, p2:
            resp_nonexistent = client.post(
                "/api/regex/search",
                json={
                    "pattern": r"def\s+\w+",
                    "repository_alias": "totally-unknown-repo-global",
                },
            )

        assert resp_real.status_code == 403
        assert resp_nonexistent.status_code == 403
        assert (
            resp_real.json()["detail"]["error_code"]
            == resp_nonexistent.json()["detail"]["error_code"]
            == "access_denied"
        )

    def test_access_filtering_service_unavailable_fails_closed(self, group_db_path):
        """access_filtering_service missing from app.state -> exactly 500,
        never a silent pass-through to search results."""
        user = _make_user("some_user")
        app = FastAPI()
        app.include_router(regex_routes.router)
        app.dependency_overrides[get_current_user] = lambda: user
        # No app.state.access_filtering_service set at all.
        client = TestClient(app)

        p1, p2 = _patch_search_backend()
        with p1 as mock_resolve, p2 as mock_search_cls:
            resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 500
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
        mock_resolve.assert_not_called()
        mock_search_cls.assert_not_called()


class TestAccessCheckOffloadedOffEventLoop:
    """This route handler is `async def` (regex_search). The repo-access
    check calls require_repo_access(), which performs synchronous DB reads
    (is_admin_user()/get_accessible_repos()) -- these must never run
    directly on the event-loop thread inside an `async def` (this project's
    own Production Scale invariant: NEVER call a synchronous DB/filesystem
    function directly inside async def). The check must be offloaded via
    anyio.to_thread.run_sync, exactly like every other synchronous call in
    this same route handler already is (see _resolve_repo_path_offloaded /
    _execute_single_search's RegexSearchService construction above it).

    NOTE: a naive "was anyio.to_thread.run_sync called at all" count is NOT
    discriminating here -- FastAPI's own dependency injection offloads
    synchronous dependency callables (e.g. this test's `lambda: user`
    override for get_current_user) through the SAME global
    anyio.to_thread.run_sync, so that count is > 0 even when the access
    check itself is never offloaded. This test instead compares the actual
    THREAD each call runs on: `User.has_permission()` is called directly in
    the coroutine (never offloaded) and calibrates "the event-loop thread's
    identity" for this request; require_repo_access() must run on a
    DIFFERENT thread."""

    def test_denied_request_offloads_access_check_off_event_loop_thread(
        self, group_db_path
    ):
        import threading

        from code_indexer.server.auth.user_manager import User as _UserCls

        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        thread_idents: dict = {}
        real_has_permission = _UserCls.has_permission
        real_require_repo_access = regex_routes.require_repo_access

        def _recording_has_permission(self_user, *a, **kw):
            thread_idents["event_loop"] = threading.get_ident()
            return real_has_permission(self_user, *a, **kw)

        def _recording_require_repo_access(*a, **kw):
            thread_idents["access_check"] = threading.get_ident()
            return real_require_repo_access(*a, **kw)

        p1, p2 = _patch_search_backend()
        with (
            p1 as mock_resolve,
            p2,
            patch.object(_UserCls, "has_permission", _recording_has_permission),
            patch(
                "code_indexer.server.routes.regex_routes.require_repo_access",
                _recording_require_repo_access,
            ),
        ):
            resp = client.post("/api/regex/search", json=VALID_SINGLE_BODY)

        assert resp.status_code == 403
        mock_resolve.assert_not_called()
        assert "event_loop" in thread_idents, "has_permission() was never called"
        assert "access_check" in thread_idents, "require_repo_access() was never called"
        assert thread_idents["access_check"] != thread_idents["event_loop"], (
            "expected require_repo_access() to run on a DIFFERENT thread than "
            "the event loop (offloaded via anyio.to_thread.run_sync) -- it "
            "performs synchronous DB reads and must never block the event "
            "loop directly inside this `async def` route; running on the "
            "SAME thread as has_permission() means the access check is NOT "
            "offloaded"
        )
