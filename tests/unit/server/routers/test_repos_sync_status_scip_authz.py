"""Body-named repository sync, the repository status summary and the SCIP
single-repository routes require group access to the golden repositories
they name or count, and refuse with 500 access_control_unavailable when the
access service is missing.

Real services from repo_authz_test_env; nothing about the access decision
is mocked. "No job submitted" is asserted on the submissions recorded by
the env's job runner (RecordingJobManager).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.routers import scip_queries
from tests.unit.server.routers.repo_authz_test_env import (
    ADMIN_USERNAME,
    GRANTED_REPO,
    POWER_USERNAME,
    UNGRANTED_REPO,
    admin,
    env,  # noqa: F401  (pytest fixture)
    power_user,
)


def _sync(client, alias: str):
    return client.post("/api/repos/sync", json={"repository_alias": alias})


class TestBodySyncRequiresGroupAccess:
    def test_ungranted_alias_is_refused_and_submits_no_job(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = _sync(client, UNGRANTED_REPO)

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert UNGRANTED_REPO in detail["detail"]
        assert env.submitted_jobs() == []

    def test_unknown_alias_is_refused_like_an_ungranted_one(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = _sync(client, "no-such-repo")

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"
        assert env.submitted_jobs() == []

    def test_granted_alias_sync_is_submitted(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = _sync(client, GRANTED_REPO)

        assert resp.status_code == 202, resp.text
        jobs = env.submitted_jobs()
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["user"] == POWER_USERNAME
        assert jobs[0]["operation_type"] == "sync_repository"
        assert jobs[0]["repo_alias"] == GRANTED_REPO

    def test_admin_sync_is_submitted_without_group_grant(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = _sync(client, UNGRANTED_REPO)

        assert resp.status_code == 202, resp.text
        assert [j["user"] for j in env.submitted_jobs()] == [ADMIN_USERNAME]
        jobs = env.submitted_jobs()
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["operation_type"] == "sync_repository"
        assert jobs[0]["repo_alias"] == UNGRANTED_REPO

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed_and_submits_no_job(
        self,
        env,  # noqa: F811
        make_user,
    ):
        client = env.client(make_user(), None)
        resp = _sync(client, GRANTED_REPO)

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
        assert env.submitted_jobs() == []


SCIP_ROUTES = [
    ("/scip/definition", {"symbol": "Example"}),
    ("/scip/references", {"symbol": "Example"}),
    ("/scip/dependencies", {"symbol": "Example"}),
    ("/scip/dependents", {"symbol": "Example"}),
    ("/scip/impact", {"symbol": "Example"}),
    ("/scip/callchain", {"from_symbol": "Example", "to_symbol": "Other"}),
    ("/scip/context", {"symbol": "Example"}),
]


def _scip_client(user, access_service, golden_repos_dir: str) -> TestClient:
    app = FastAPI()
    app.include_router(scip_queries.router)
    app.state.access_filtering_service = access_service
    app.state.golden_repos_dir = golden_repos_dir
    app.dependency_overrides[dependencies.get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,params", SCIP_ROUTES)
class TestScipSingleRepoRoutesRequireAccessService:
    """Every SCIP single-repository route filters by the caller's group
    access, so it refuses to run without the access service."""

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(
        self,
        env,  # noqa: F811
        path,
        params,
        make_user,
    ):
        client = _scip_client(
            make_user(), None, env.golden_repo_manager.golden_repos_dir
        )
        resp = client.get(path, params={**params, "project": GRANTED_REPO})

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"

    def test_route_runs_with_access_service(self, env, path, params):  # noqa: F811
        client = _scip_client(
            power_user(), env.access_service, env.golden_repo_manager.golden_repos_dir
        )
        resp = client.get(path, params={**params, "project": GRANTED_REPO})

        assert resp.status_code == 200, resp.text


class TestStatusSummaryCountsOnlyAccessibleRepos:
    """The registry holds two golden repositories; the power user is
    granted one of them."""

    def test_non_admin_counts_only_granted_repos(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get("/api/repos/status")

        assert resp.status_code == 200, resp.text
        available = resp.json()["available_repositories"]
        assert available == {"total_count": 1, "not_activated_count": 1}

    def test_admin_counts_every_repo(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.get("/api/repos/status")

        assert resp.status_code == 200, resp.text
        available = resp.json()["available_repositories"]
        assert available == {"total_count": 2, "not_activated_count": 2}

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get("/api/repos/status")

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
