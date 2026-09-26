"""Regression tests verifying repo-level access control for GET
/api/repositories/{repo_alias}/description, POST .../health/check, and GET
.../indexes. Each route must verify the caller's group-based repository
grant before returning any repo-scoped data or submitting a background
health-check job for repo_alias, matching the coarse `query_repos`-style
gate other repository routes already apply.

Front door: real FastAPI TestClient against repository_health.router, with a
REAL AccessFilteringService backed by a REAL GroupAccessManager (temp SQLite
DB) -- not a mock of the access-control decision itself.

repository_health.py's manager-lookup helpers (_get_golden_repo_manager /
_get_activated_repo_manager / _get_background_job_manager) read from the
GLOBAL `code_indexer.server.app.app.state` singleton, not the request-scoped
`request.app.state` (see test_repository_health_async_job_1394.py's own
docstring) -- the new _get_access_filtering_service() getter follows that
SAME established pattern for consistency and testability, so it is patched
here exactly like its siblings rather than set on a locally-built app's
`app.state`.

Repository aliases and usernames are neutral placeholders (this is a public
open-source repository).

Scenarios (each of the 3 routes):
- normal_user without the repo's group grant -> 403, no filesystem/job work
  reached
- admin user bypasses the check entirely -> success
- normal_user WITH the repo's group grant -> success
- access_filtering_service missing -> exactly 500 (fails closed)
- the access check itself runs off the event-loop thread via
  anyio.to_thread.run_sync (all three routes are `async def`)
"""

from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.routers import repository_health


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
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(granted_username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")

    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group(admin_username, admins_group.id, assigned_by="test")

    return AccessFilteringService(gam)


def _build_app(user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(repository_health.router)
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    return app


def _make_golden_repo_manager(known_alias: str = "example-repo"):
    mock_grm = MagicMock()
    mock_grm.get_golden_repo.side_effect = (
        lambda alias: object() if alias == known_alias else None
    )
    mock_grm.get_actual_repo_path.side_effect = lambda alias: "/some/repo/path"
    return mock_grm


def _patch_backend(known_alias: str = "example-repo"):
    """Patch the filesystem/job-manager collaborators so a request that
    WRONGLY passes the access check would still reach real "results" --
    exactly the shape the RED test must prove never happens."""
    mock_grm = _make_golden_repo_manager(known_alias)
    mock_bjm = MagicMock()
    mock_bjm.submit_job.return_value = "job-1"
    return (
        patch.object(
            repository_health, "_get_golden_repo_manager", return_value=mock_grm
        ),
        patch.object(
            repository_health, "_get_activated_repo_manager", return_value=MagicMock()
        ),
        patch.object(
            repository_health, "_get_background_job_manager", return_value=mock_bjm
        ),
    )


class TestDescriptionRepoAuthz:
    ROUTE = "/api/repositories/example-repo/description"

    def test_denied_for_ungranted_user_returns_403(self, group_db_path, tmp_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user)
        app.state.golden_repos_dir = str(tmp_path)
        client = TestClient(app)

        with patch.object(
            repository_health,
            "_get_access_filtering_service",
            return_value=access_service,
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"

    def test_admin_bypasses_check(self, group_db_path, tmp_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin)
        cidx_meta_dir = tmp_path / "cidx-meta"
        cidx_meta_dir.mkdir()
        (cidx_meta_dir / "example-repo.md").write_text("# hello\n")
        app.state.golden_repos_dir = str(tmp_path)
        client = TestClient(app)

        with patch.object(
            repository_health,
            "_get_access_filtering_service",
            return_value=access_service,
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 200, resp.text

    def test_granted_user_succeeds(self, group_db_path, tmp_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user)
        cidx_meta_dir = tmp_path / "cidx-meta"
        cidx_meta_dir.mkdir()
        (cidx_meta_dir / "example-repo.md").write_text("# hello\n")
        app.state.golden_repos_dir = str(tmp_path)
        client = TestClient(app)

        with patch.object(
            repository_health,
            "_get_access_filtering_service",
            return_value=access_service,
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 200, resp.text

    def test_access_filtering_service_unavailable_fails_closed(self, tmp_path):
        user = _make_user("some_user")
        app = _build_app(user)
        app.state.golden_repos_dir = str(tmp_path)
        client = TestClient(app)

        with patch.object(
            repository_health, "_get_access_filtering_service", return_value=None
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestHealthCheckRepoAuthz:
    ROUTE = "/api/repositories/example-repo/health/check"

    def test_denied_for_ungranted_user_returns_403_no_job_submitted(
        self, group_db_path
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3 as mock_bjm_factory,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.post(self.ROUTE)

        assert resp.status_code == 403, resp.text
        mock_bjm_factory.assert_not_called()

    def test_admin_bypasses_check(self, group_db_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.post(self.ROUTE)

        assert resp.status_code == 202, resp.text

    def test_granted_user_succeeds(self, group_db_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.post(self.ROUTE)

        assert resp.status_code == 202, resp.text

    def test_access_filtering_service_unavailable_fails_closed(self):
        user = _make_user("some_user")
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3 as mock_bjm_factory,
            patch.object(
                repository_health, "_get_access_filtering_service", return_value=None
            ),
        ):
            resp = client.post(self.ROUTE)

        assert resp.status_code == 500, resp.text
        mock_bjm_factory.assert_not_called()


class TestIndexesRepoAuthz:
    ROUTE = "/api/repositories/example-repo/indexes"

    def test_denied_for_ungranted_user_returns_403(self, group_db_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1 as mock_grm_factory,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 403, resp.text
        # get_golden_repo() is a necessary part of resolving WHICH target
        # to authorise; get_actual_repo_path() is the actual filesystem
        # resolution step and must never run for a denied request.
        mock_grm_factory.return_value.get_actual_repo_path.assert_not_called()

    def test_admin_bypasses_check(self, group_db_path, tmp_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 200, resp.text

    def test_granted_user_succeeds(self, group_db_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 200, resp.text

    def test_access_filtering_service_unavailable_fails_closed(self):
        user = _make_user("some_user")
        app = _build_app(user)
        client = TestClient(app)

        p1, p2, p3 = _patch_backend()
        with (
            p1 as mock_grm_factory,
            p2,
            p3,
            patch.object(
                repository_health, "_get_access_filtering_service", return_value=None
            ),
        ):
            resp = client.get(self.ROUTE)

        assert resp.status_code == 500, resp.text
        mock_grm_factory.assert_not_called()


def _make_activated_repo_manager_with_backing_golden(
    clone_path: Path,
    *,
    known_alias: str,
    backing_golden_alias: Optional[str],
) -> MagicMock:
    """Activated repo manager resolving ONE known custom user_alias to
    clone_path, with get_repository() reporting the BACKING golden repo
    alias (or None for a composite repo with no single backing golden
    repo) -- the exact shape _resolve_golden_repo_alias_for_activated_repo
    reads to authorise the fallback."""
    mock_arm = MagicMock()

    def _get_path(username: str, user_alias: str) -> str:
        if user_alias != known_alias:
            raise FileNotFoundError(user_alias)
        return str(clone_path)

    def _get_repository(username: str, user_alias: str, *, touch: bool = True):
        if user_alias != known_alias:
            return None
        return {"user_alias": user_alias, "golden_repo_alias": backing_golden_alias}

    mock_arm.get_activated_repo_path.side_effect = _get_path
    mock_arm.get_repository.side_effect = _get_repository
    return mock_arm


class TestActivatedRepoBackingGoldenAliasFallback:
    """These routes ALSO resolve the caller's OWN activated repo by its
    custom alias (_resolve_repository_path strategy 3 for health/check,
    the equivalent inline strategy 3 for indexes). A user's chosen custom
    alias for an activated repo generally differs from the golden repo
    name backing it, so access must be authorised via
    _resolve_golden_repo_alias_for_activated_repo() when the caller holds
    the BACKING golden repo's grant, even though the custom alias itself
    is not a golden-repo grant.

    Scenario: a NORMAL_USER granted 'backing-golden' calls GET
    .../my-activated-repo/indexes and POST
    .../my-activated-repo/health/check on their own activated repo -- both
    must succeed.
    """

    ACTIVATED_ALIAS = "my-activated-repo"
    BACKING_GOLDEN_ALIAS = "backing-golden"

    @pytest.mark.parametrize(
        "method,route_suffix",
        [("get", "indexes"), ("post", "health/check")],
    )
    def test_allowed_when_backing_golden_repo_granted(
        self, group_db_path, tmp_path, method, route_suffix
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=[self.BACKING_GOLDEN_ALIAS],
        )
        app = _build_app(user)
        client = TestClient(app)

        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_grm = MagicMock()
        mock_grm.get_golden_repo.return_value = None  # not a golden repo itself
        mock_arm = _make_activated_repo_manager_with_backing_golden(
            clone_path,
            known_alias=self.ACTIVATED_ALIAS,
            backing_golden_alias=self.BACKING_GOLDEN_ALIAS,
        )
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-1"

        with (
            patch.object(
                repository_health, "_get_golden_repo_manager", return_value=mock_grm
            ),
            patch.object(
                repository_health,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            ),
            patch.object(
                repository_health,
                "_get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.request(
                method, f"/api/repositories/{self.ACTIVATED_ALIAS}/{route_suffix}"
            )

        assert resp.status_code in (200, 202), resp.text

    @pytest.mark.parametrize(
        "method,route_suffix",
        [("get", "indexes"), ("post", "health/check")],
    )
    def test_denied_when_backing_golden_repo_not_granted(
        self, group_db_path, tmp_path, method, route_suffix
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["unrelated-repo"],
        )
        app = _build_app(user)
        client = TestClient(app)

        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_grm = MagicMock()
        mock_grm.get_golden_repo.return_value = None
        mock_arm = _make_activated_repo_manager_with_backing_golden(
            clone_path,
            known_alias=self.ACTIVATED_ALIAS,
            backing_golden_alias=self.BACKING_GOLDEN_ALIAS,
        )
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-1"

        with (
            patch.object(
                repository_health, "_get_golden_repo_manager", return_value=mock_grm
            ),
            patch.object(
                repository_health,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            ),
            patch.object(
                repository_health,
                "_get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.request(
                method, f"/api/repositories/{self.ACTIVATED_ALIAS}/{route_suffix}"
            )

        assert resp.status_code == 403, resp.text
        mock_bjm.submit_job.assert_not_called()

    def test_denied_when_no_single_backing_golden_repo_composite(
        self, group_db_path, tmp_path
    ):
        """A composite repo with no single backing golden repo
        (_resolve_golden_repo_alias_for_activated_repo returns None) must
        still be denied -- never silently allowed just because the
        fallback lookup came back empty."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=[]
        )
        app = _build_app(user)
        client = TestClient(app)

        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_grm = MagicMock()
        mock_grm.get_golden_repo.return_value = None
        mock_arm = _make_activated_repo_manager_with_backing_golden(
            clone_path,
            known_alias=self.ACTIVATED_ALIAS,
            backing_golden_alias=None,  # composite repo -- no single backing repo
        )

        with (
            patch.object(
                repository_health, "_get_golden_repo_manager", return_value=mock_grm
            ),
            patch.object(
                repository_health,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            ),
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.get(f"/api/repositories/{self.ACTIVATED_ALIAS}/indexes")

        assert resp.status_code == 403, resp.text


class TestAccessCheckOffloadedOffEventLoop:
    """All three routes are `async def`. The access check performs
    synchronous DB reads (is_admin_user()/get_accessible_repos()) which must
    never run directly on the event-loop thread (Production Scale
    invariant) -- offload via anyio.to_thread.run_sync.

    Calibration: override get_current_user_hybrid with an ASYNC function
    (not a plain lambda) so FastAPI awaits it directly on the event loop
    rather than offloading it itself via run_in_threadpool (which is what
    happens to SYNC dependency callables and would contaminate a naive
    "any thread offload happened" count)."""

    @pytest.mark.parametrize(
        "method,route",
        [
            ("get", "/api/repositories/example-repo/description"),
            ("post", "/api/repositories/example-repo/health/check"),
            ("get", "/api/repositories/example-repo/indexes"),
        ],
    )
    def test_denied_request_offloads_access_check_off_event_loop_thread(
        self, group_db_path, tmp_path, method, route
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path, granted_username=user.username, granted_repos=["other-repo"]
        )

        app = FastAPI()
        app.include_router(repository_health.router)
        app.state.golden_repos_dir = str(tmp_path)

        thread_idents: dict = {}

        async def _probe_user():
            thread_idents["event_loop"] = threading.get_ident()
            return user

        app.dependency_overrides[get_current_user_hybrid] = _probe_user
        client = TestClient(app)

        real_require_repo_access = repository_health.require_repo_access

        def _recording_require_repo_access(*a, **kw):
            thread_idents["access_check"] = threading.get_ident()
            return real_require_repo_access(*a, **kw)

        p1, p2, p3 = _patch_backend()
        with (
            p1,
            p2,
            p3,
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
            patch.object(
                repository_health,
                "require_repo_access",
                _recording_require_repo_access,
            ),
        ):
            resp = client.request(method, route)

        assert resp.status_code == 403, resp.text
        assert "event_loop" in thread_idents, "get_current_user_hybrid never ran"
        assert "access_check" in thread_idents, "require_repo_access never ran"
        assert thread_idents["access_check"] != thread_idents["event_loop"], (
            f"{route}: expected require_repo_access() to run on a DIFFERENT "
            "thread than the event loop (offloaded via "
            "anyio.to_thread.run_sync) -- running on the SAME thread means "
            "the access check is NOT offloaded"
        )


class TestActivatedAliasCollisionWithGoldenRepo:
    """Activation records a caller-chosen custom alias for the activated
    repo with no check against existing golden repo aliases. Each route's
    own resolution order (golden repo first, then the caller's own
    activated repo) must be the SAME order authorisation uses -- so a
    caller who activates a golden repo they hold under a custom alias
    that happens to equal a DIFFERENT, ungranted golden repo's alias must
    still be denied for that alias: the route resolves it as the
    ungranted golden repo, not as the caller's own activation.
    """

    COLLIDING_ALIAS = "collision-golden"  # both the custom alias AND an
    # existing golden repo's alias the caller has NO grant for
    GRANTED_GOLDEN = "granted-golden"  # the repo actually backing the
    # caller's activation under COLLIDING_ALIAS

    def _make_managers(self, tmp_path: Path, activated_alias: str):
        """Build golden/activated manager mocks. `activated_alias` is the
        EXACT custom alias the caller activated their granted golden repo
        under -- for the plain-alias case this equals COLLIDING_ALIAS
        itself; for the `-global`-suffixed case it is
        `f"{COLLIDING_ALIAS}-global"`, an activated alias that is NOT
        itself a golden repo but strips down to one the caller lacks --
        this is what actually exercises the `-global`-strip branch in
        _resolve_repo_access_target rather than merely missing an
        activated-repo lookup for an alias nothing ever registers."""
        mock_grm = MagicMock()
        mock_grm.get_golden_repo.side_effect = (
            lambda alias: object()
            if alias in (self.COLLIDING_ALIAS, self.GRANTED_GOLDEN)
            else None
        )
        mock_grm.get_actual_repo_path.return_value = str(tmp_path / "golden-clone")

        mock_arm = MagicMock()
        activated_clone = tmp_path / "activated-clone"
        activated_clone.mkdir(parents=True, exist_ok=True)

        def _get_path(username: str, user_alias: str) -> str:
            if user_alias == activated_alias:
                return str(activated_clone)
            raise FileNotFoundError(user_alias)

        def _get_repository(username: str, user_alias: str, *, touch: bool = True):
            if user_alias == activated_alias:
                return {
                    "user_alias": user_alias,
                    "golden_repo_alias": self.GRANTED_GOLDEN,
                }
            return None

        mock_arm.get_activated_repo_path.side_effect = _get_path
        mock_arm.get_repository.side_effect = _get_repository
        return mock_grm, mock_arm

    @pytest.mark.parametrize("alias_suffix", ["", "-global"])
    @pytest.mark.parametrize(
        "method,route_suffix",
        [("get", "indexes"), ("post", "health/check")],
    )
    def test_colliding_alias_denied_for_indexes_and_health(
        self, group_db_path, tmp_path, method, route_suffix, alias_suffix
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=[self.GRANTED_GOLDEN],
        )
        app = _build_app(user)
        client = TestClient(app)

        # The caller activated their GRANTED golden repo under this exact
        # custom alias -- for the `-global` case this alias is itself NOT
        # a golden repo, but strips down to COLLIDING_ALIAS, an ungranted
        # one.
        activated_alias = f"{self.COLLIDING_ALIAS}{alias_suffix}"
        mock_grm, mock_arm = self._make_managers(tmp_path, activated_alias)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-1"

        with (
            patch.object(
                repository_health, "_get_golden_repo_manager", return_value=mock_grm
            ),
            patch.object(
                repository_health,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            ),
            patch.object(
                repository_health,
                "_get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.request(
                method,
                f"/api/repositories/{self.COLLIDING_ALIAS}{alias_suffix}/{route_suffix}",
            )

        assert resp.status_code == 403, resp.text
        mock_bjm.submit_job.assert_not_called()

    def test_colliding_alias_denied_for_description(self, group_db_path, tmp_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=[self.GRANTED_GOLDEN],
        )
        app = _build_app(user)
        cidx_meta_dir = tmp_path / "cidx-meta"
        cidx_meta_dir.mkdir()
        (cidx_meta_dir / f"{self.COLLIDING_ALIAS}.md").write_text("# secret\n")
        app.state.golden_repos_dir = str(tmp_path)
        client = TestClient(app)

        mock_grm, mock_arm = self._make_managers(tmp_path, self.COLLIDING_ALIAS)

        with (
            patch.object(
                repository_health, "_get_golden_repo_manager", return_value=mock_grm
            ),
            patch.object(
                repository_health,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            ),
            patch.object(
                repository_health,
                "_get_access_filtering_service",
                return_value=access_service,
            ),
        ):
            resp = client.get(f"/api/repositories/{self.COLLIDING_ALIAS}/description")

        assert resp.status_code == 403, resp.text
