"""History of the internal catalog is admin-only on the REST branch routes.

Branch listings carry commit metadata and a branch switch moves a clone
through history, so these routes refuse the catalog to restricted callers
(judged by the golden identity of the repository served) and keep it for
admins. The real repository routes on a small FastAPI app, a real git
repository for the catalog, a real ActivatedRepoManager holding catalog
activations, and the real group store. The golden-side managers are
recording doubles: the refusal must come before any of them is asked.
Neutral names only.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_repos import register_repo_routes
from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes
from code_indexer.server.services.cidx_meta_access import acting_scope
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)
from tests.unit.server.mcp.test_meta_history_surfaces_denied import (
    _activate,
    _make_repo,
)

CATALOG_CLONE = "team-notes"
COMPOSITE = "mixed-notes"
GIT_REFUSAL = CatalogSurface.GIT_HISTORY.refusal
COMMIT_TEXT = "retire descriptions"


class RecordingGoldenSide:
    """Golden repository manager and listing manager double: records every
    question and answers with the catalog's real clone path."""

    def __init__(self, clone_path: Path) -> None:
        self.clone_path = clone_path
        self.calls: List[str] = []

    def golden_repo_exists(self, alias: str) -> bool:
        self.calls.append(f"exists:{alias}")
        return True

    def get_golden_repo_branches(self, alias: str) -> List[Any]:
        self.calls.append(f"branches:{alias}")
        return []

    def get_repository_details(self, alias: str, username: str) -> Dict[str, Any]:
        self.calls.append(f"details:{alias}")
        return {"clone_path": str(self.clone_path)}


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name, password_hash="hash", role=role, created_at=datetime.now()
    )


@pytest.fixture(scope="module")
def module_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("meta-branch-rest")


@pytest.fixture(scope="module")
def fx(module_tmp: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(module_tmp)


@pytest.fixture(scope="module")
def server(module_tmp: Path, fx: CidxMetaFixture) -> Iterator[SimpleNamespace]:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    catalog = module_tmp / "sources" / "cidx-meta"
    _make_repo(
        catalog, {f"{ALPHA}.md": f"# {ALPHA}\n", "notes.md": "x\n"}, ["notes.md"]
    )
    data_dir = module_tmp / "server" / "data"
    for username in (RESTRICTED_USER, ADMIN_USER):
        _activate(data_dir, catalog, username, CATALOG_CLONE)
        _activate(data_dir, catalog, username, COMPOSITE)
        meta = data_dir / "activated-repos" / username / f"{COMPOSITE}_metadata.json"
        record = json.loads(meta.read_text())
        record.update(
            {"is_composite": True, "golden_repo_aliases": [ALPHA, "cidx-meta"]}
        )
        meta.write_text(json.dumps(record))

    arm = ActivatedRepoManager(data_dir=str(data_dir))
    golden = RecordingGoldenSide(catalog)
    current: Dict[str, Any] = {"scope": None}
    app = FastAPI()
    app.state.access_filtering_service = fx.access_service

    @app.middleware("http")
    async def _acting(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The MCP dispatcher's acting-user scope, bound per request.
        with acting_scope(current["scope"]):
            return await call_next(request)

    register_repo_routes(
        app,
        activated_repo_manager=arm,
        golden_repo_manager=golden,
        repository_listing_manager=golden,
        background_job_manager=None,
    )
    register_repos_v2_routes(
        app,
        activated_repo_manager=arm,
        repository_listing_manager=golden,
        background_job_manager=None,
    )
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            client=client, current=current, arm=arm, golden=golden, data_dir=data_dir
        )


def _get(
    server: SimpleNamespace,
    path: str,
    username: str,
    scope: Optional[List[str]] = None,
) -> Any:
    # Role ADMIN passes role gates; visibility is admins-group membership
    # (narrowed by an acting scope), so RESTRICTED_USER stays restricted.
    server.current["user"] = _user(username, UserRole.ADMIN)
    server.current["scope"] = scope
    server.golden.calls.clear()
    return server.client.get(path)


def _assert_no_history(server: SimpleNamespace, response: Any) -> None:
    assert COMMIT_TEXT not in response.text
    assert server.golden.calls == []


BRANCH_LISTINGS = [
    f"/api/repos/{CATALOG_CLONE}/branches",
    f"/api/repos/{COMPOSITE}/branches",
    "/api/repos/golden/cidx-meta/branches",
    "/api/repos/golden/cidx-meta-global/branches",
    "/api/repositories/cidx-meta-global/branches",
    "/api/repositories/cidx-meta/branches",
    f"/api/repositories/{CATALOG_CLONE}/branches",
    f"/api/repositories/{COMPOSITE}/branches",
]


@pytest.mark.parametrize("path", BRANCH_LISTINGS)
def test_branch_listing_refuses_the_catalog_for_restricted_caller(
    server: SimpleNamespace, path: str
) -> None:
    # The activation rule may answer first (as on the git router); either
    # way nothing of the catalog's history is served.
    response = _get(server, path, RESTRICTED_USER)
    assert response.status_code in (403, 404), response.text
    _assert_no_history(server, response)


@pytest.mark.parametrize("path", BRANCH_LISTINGS)
def test_branch_listing_history_refusal_stands_on_its_own(
    server: SimpleNamespace, path: str
) -> None:
    # An admin acting for a user passes the activation and grant rules but
    # is restricted to that user's scope: the history refusal alone answers.
    response = _get(server, path, ADMIN_USER, scope=[ALPHA])
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": GIT_REFUSAL}
    _assert_no_history(server, response)


@pytest.mark.parametrize(
    "path",
    [
        f"/api/repos/{CATALOG_CLONE}/branches",
        f"/api/repositories/{CATALOG_CLONE}/branches",
    ],
)
def test_admin_lists_catalog_clone_branches(server: SimpleNamespace, path: str) -> None:
    response = _get(server, path, ADMIN_USER)
    assert response.status_code == 200, response.text
    assert COMMIT_TEXT in response.text


def test_admin_lists_golden_catalog_branches(server: SimpleNamespace) -> None:
    response = _get(server, "/api/repos/golden/cidx-meta-global/branches", ADMIN_USER)
    assert response.status_code == 200, response.text
    assert server.golden.calls == [
        "exists:cidx-meta-global",
        "branches:cidx-meta-global",
    ]


def _clone_git(server: SimpleNamespace, username: str, *args: str) -> str:
    import subprocess

    clone = server.data_dir / "activated-repos" / username / CATALOG_CLONE
    done = subprocess.run(
        ["git", *args], cwd=clone, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _put_branch(
    server: SimpleNamespace,
    username: str,
    branch: str,
    create: bool,
    scope: Optional[List[str]],
) -> Any:
    server.current["user"] = _user(username, UserRole.ADMIN)
    server.current["scope"] = scope
    return server.client.put(
        f"/api/repos/{CATALOG_CLONE}/branch",
        json={"branch_name": branch, "create": create},
    )


def _with_older_branch_on_main(server: SimpleNamespace, username: str) -> None:
    _clone_git(server, username, "checkout", "-q", "main")
    if not _clone_git(server, username, "branch", "--list", "older"):
        _clone_git(server, username, "branch", "older", "HEAD~1")


# (username, acting scope, history refusal must answer by itself)
_RESTRICTED_CALLERS = {
    "restricted": (RESTRICTED_USER, None, False),
    "admin-acting-for-user": (ADMIN_USER, [ALPHA], True),
}


@pytest.mark.parametrize("caller", sorted(_RESTRICTED_CALLERS))
@pytest.mark.parametrize(
    "branch, create",
    [("older", False), ("fresh-start", True)],
    ids=["switch", "create"],
)
def test_branch_switch_refuses_a_catalog_clone(
    server: SimpleNamespace, caller: str, branch: str, create: bool
) -> None:
    username, scope, exact = _RESTRICTED_CALLERS[caller]
    _with_older_branch_on_main(server, username)
    before = _clone_git(server, username, "branch", "--list")
    response = _put_branch(server, username, branch, create, scope)
    if exact:
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": GIT_REFUSAL}
    else:
        assert response.status_code in (403, 404), response.text
    assert _clone_git(server, username, "branch", "--show-current") == "main"
    assert _clone_git(server, username, "branch", "--list") == before


def test_admin_switches_own_catalog_clone_branch(server: SimpleNamespace) -> None:
    # ADMIN_USER with no acting scope is unrestricted.
    _with_older_branch_on_main(server, ADMIN_USER)
    response = _put_branch(server, ADMIN_USER, "older", False, None)
    try:
        assert response.status_code == 200, response.text
        assert _clone_git(server, ADMIN_USER, "branch", "--show-current") == "older"
    finally:
        _clone_git(server, ADMIN_USER, "checkout", "-q", "main")


class RecordingJobs:
    def __init__(self) -> None:
        self.submitted: List[str] = []

    def submit_job(self, operation_type: str, *args: Any, **kwargs: Any) -> str:
        self.submitted.append(operation_type)
        return "job-1"


@pytest.fixture(scope="module")
def activated_door(
    server: SimpleNamespace, fx: CidxMetaFixture
) -> Iterator[SimpleNamespace]:
    from code_indexer.server.auth.dependencies import get_current_user_hybrid
    from code_indexer.server.routers import activated_repos, repo_access_http

    jobs = RecordingJobs()
    current: Dict[str, Any] = {"scope": None}
    app = FastAPI()

    @app.middleware("http")
    async def _acting(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        with acting_scope(current["scope"]):
            return await call_next(request)

    app.include_router(activated_repos.router)
    app.dependency_overrides[get_current_user_hybrid] = lambda: current["user"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(activated_repos, "_get_activated_repo_manager", lambda: server.arm)
        mp.setattr(activated_repos, "_get_background_job_manager", lambda: jobs)
        mp.setattr(
            repo_access_http,
            "module_app_access_filtering_service",
            lambda: fx.access_service,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield SimpleNamespace(client=client, current=current, jobs=jobs)


def _activated(
    door: SimpleNamespace,
    method: str,
    suffix: str,
    username: str,
    scope: Optional[List[str]],
) -> Any:
    door.current["user"] = _user(username, UserRole.ADMIN)
    door.current["scope"] = scope
    door.jobs.submitted.clear()
    body = {"branch_name": "older"} if method == "POST" else None
    return door.client.request(
        method, f"/api/activated-repos/{CATALOG_CLONE}/{suffix}", json=body
    )


@pytest.mark.parametrize("caller", sorted(_RESTRICTED_CALLERS))
@pytest.mark.parametrize("method, suffix", [("GET", "branches"), ("POST", "branch")])
def test_activated_repo_branch_routes_refuse_a_catalog_clone(
    activated_door: SimpleNamespace, caller: str, method: str, suffix: str
) -> None:
    username, scope, exact = _RESTRICTED_CALLERS[caller]
    response = _activated(activated_door, method, suffix, username, scope)
    if exact:
        assert response.status_code == 403, response.text
        assert response.json() == {"detail": GIT_REFUSAL}
    else:
        assert response.status_code in (403, 404), response.text
    assert activated_door.jobs.submitted == []


def test_admin_lists_activated_catalog_clone_branches(
    activated_door: SimpleNamespace,
) -> None:
    response = _activated(activated_door, "GET", "branches", ADMIN_USER, None)
    assert response.status_code == 200, response.text
    assert "main" in response.json()["branches"]
