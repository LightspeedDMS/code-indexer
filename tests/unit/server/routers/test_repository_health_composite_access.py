"""Repository health routes: the caller's OWN activated composite repository
is authorised through its component golden repositories.

POST /api/repositories/{alias}/health/check and GET .../indexes resolve the
caller's own activated repositories (strategy 3), which includes composite
repositories created via manage_composite_repository. Such a composite is
served iff the caller has access to EVERY component golden repository
(admins bypass). Another user's composite, or a composite with any
inaccessible component (or no recorded components), stays denied with the
route's existing 403 response. GET .../description reads golden-keyed
cidx-meta and stays direct-access-only.

Front door: real FastAPI TestClient against repository_health.router, a
REAL AccessFilteringService backed by a REAL GroupAccessManager (temp SQLite
DB), and a REAL ActivatedRepoManager whose composite metadata lives on disk
in the same JSON layout composite activation writes. Only the golden repo
manager (a configurable set of existing golden repos -- by default exactly
the components) and the background job manager (to observe job submission)
are stubbed. A component that no longer exists as a golden repo denies the
composite even when a grant for its alias remains.

Aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.routers import repository_health
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager

COMPOSITE_ALIAS = "my-composite"
COMPONENTS = ["example-repo-a", "example-repo-b"]
OWNER = "alice"
OTHER_USER = "bob"
ADMIN = "admin_user"

ROUTES = [("get", "indexes"), ("post", "health/check")]


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _build_access_service(db_path: Path, granted_repos: List[str]):
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    for username in (OWNER, OTHER_USER):
        gam.assign_user_to_group(username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")
    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None
    gam.assign_user_to_group(ADMIN, admins_group.id, assigned_by="test")
    return AccessFilteringService(gam)


def _write_composite(
    arm: ActivatedRepoManager, username: str, components: list
) -> Path:
    """Persist a composite activation exactly as composite activation does:
    a proxy-mode clone directory plus {alias}_metadata.json carrying
    is_composite/golden_repo_aliases."""
    composite_path = Path(arm.activated_repos_dir) / username / COMPOSITE_ALIAS
    (composite_path / ".code-indexer").mkdir(parents=True)
    (composite_path / ".code-indexer" / "config.json").write_text(
        json.dumps({"proxy_mode": True, "discovered_repos": list(components)})
    )
    arm._save_metadata_file(
        username,
        COMPOSITE_ALIAS,
        {
            "user_alias": COMPOSITE_ALIAS,
            "username": username,
            "path": str(composite_path),
            "is_composite": True,
            "golden_repo_aliases": list(components),
            "discovered_repos": list(components),
            "activated_at": "2024-01-01T00:00:00+00:00",
            "last_accessed": "2024-01-01T00:00:00+00:00",
        },
    )
    return composite_path


def _set_existing_golden_repos(arm: ActivatedRepoManager, aliases: List[str]) -> None:
    """Make exactly `aliases` exist as golden repos (golden manager lookup)."""
    existing = set(aliases)
    arm.golden_repo_manager.get_golden_repo.side_effect = (  # type: ignore[attr-defined]
        lambda alias: object() if alias in existing else None
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Tuple[ActivatedRepoManager, MagicMock, Path]]:
    grm = MagicMock()
    bjm = MagicMock()
    bjm.submit_job.return_value = "job-1"
    arm = ActivatedRepoManager(
        data_dir=str(tmp_path / "data"),
        golden_repo_manager=grm,
        background_job_manager=bjm,
    )
    # Every component exists as a golden repo; the composite alias does not.
    _set_existing_golden_repos(arm, COMPONENTS)
    yield arm, bjm, tmp_path / "groups.db"


def _call(
    arm: ActivatedRepoManager,
    bjm: MagicMock,
    access_service: AccessFilteringService,
    user: User,
    method: str,
    route_suffix: str,
):
    app = FastAPI()
    app.include_router(repository_health.router)
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    client = TestClient(app)
    with (
        patch.object(
            repository_health,
            "_get_golden_repo_manager",
            return_value=arm.golden_repo_manager,
        ),
        patch.object(
            repository_health, "_get_activated_repo_manager", return_value=arm
        ),
        patch.object(
            repository_health, "_get_background_job_manager", return_value=bjm
        ),
        patch.object(
            repository_health,
            "_get_access_filtering_service",
            return_value=access_service,
        ),
    ):
        return client.request(
            method, f"/api/repositories/{COMPOSITE_ALIAS}/{route_suffix}"
        )


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_own_composite_allowed_when_every_component_accessible(
    env, method, route_suffix
):
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, COMPONENTS)
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code in (200, 202), resp.text
    if route_suffix == "health/check":
        bjm.submit_job.assert_called_once()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_own_composite_denied_when_any_component_inaccessible(
    env, method, route_suffix
):
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, COMPONENTS)
    access = _build_access_service(db_path, granted_repos=COMPONENTS[:1])

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_own_composite_denied_when_a_component_golden_repo_no_longer_exists(
    env, method, route_suffix
):
    """A component golden repo that was removed while its group grant
    remains must not authorise the composite."""
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, COMPONENTS)
    _set_existing_golden_repos(arm, COMPONENTS[:1])
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize(
    "components",
    [[COMPONENTS[0], 123], [COMPONENTS[0], ""], [COMPONENTS[0], None]],
)
@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_own_composite_with_malformed_component_list_denied(
    env, method, route_suffix, components
):
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, components)
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_composite_alias_shadowing_an_ungranted_golden_repo_denied(
    env, method, route_suffix
):
    """The routes resolve a golden repo before the caller's activated repos,
    so an activated composite whose alias equals a golden alias the caller
    lacks is authorised (and denied) against that golden repo, never
    through the composite's components."""
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, COMPONENTS)
    _set_existing_golden_repos(arm, COMPONENTS + [COMPOSITE_ALIAS])
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_other_users_composite_denied(env, method, route_suffix):
    arm, bjm, db_path = env
    _write_composite(arm, OTHER_USER, COMPONENTS)
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_own_composite_without_recorded_components_denied(env, method, route_suffix):
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, [])
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), method, route_suffix)

    assert resp.status_code == 403, resp.text
    bjm.submit_job.assert_not_called()


@pytest.mark.parametrize("method,route_suffix", ROUTES)
def test_admin_own_composite_allowed_without_component_grants(
    env, method, route_suffix
):
    arm, bjm, db_path = env
    _write_composite(arm, ADMIN, COMPONENTS)
    access = _build_access_service(db_path, granted_repos=[])

    resp = _call(
        arm, bjm, access, _make_user(ADMIN, UserRole.ADMIN), method, route_suffix
    )

    assert resp.status_code in (200, 202), resp.text


def test_description_route_stays_direct_access_only_for_composite(env):
    arm, bjm, db_path = env
    _write_composite(arm, OWNER, COMPONENTS)
    access = _build_access_service(db_path, granted_repos=COMPONENTS)

    resp = _call(arm, bjm, access, _make_user(OWNER), "get", "description")

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "access_denied"
